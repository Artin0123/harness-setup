#!/usr/bin/env python3
"""Local telemetry & Quota Tracker backend for the agy-metrics userscript."""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path.home() / ".gemini" / "antigravity-cli"
PLUGIN_DATA_DIR = BASE_DIR / "plugin_data" / "agy-metrics"
QUOTA_DB_PATH = PLUGIN_DATA_DIR / "agy_metrics_quota.db"
CLOUDCODE_BASE = "https://daily-cloudcode-pa.googleapis.com"
USER_AGENT = "antigravity/1.5.16"


# ---------------------------------------------------------------------------
# Protobuf wire decoder (zero external deps)
# ---------------------------------------------------------------------------

def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
    raise ValueError("Truncated protobuf varint")


def parse_pb(buf: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    i = 0
    while i < len(buf):
        try:
            tag, i = read_varint(buf, i)
        except ValueError:
            break
        fn = tag >> 3
        wt_num = tag & 7
        if fn == 0:
            break
        fields.setdefault(fn, [])
        if wt_num == 0:
            v, i = read_varint(buf, i)
            fields[fn].append(v)
        elif wt_num == 2:
            l, i = read_varint(buf, i)
            if i + l > len(buf):
                break
            fields[fn].append(buf[i : i + l])
            i += l
        elif wt_num == 5:
            fields[fn].append(buf[i : i + 4])
            i += 4
        elif wt_num == 1:
            fields[fn].append(buf[i : i + 8])
            i += 8
        else:
            break
    return fields


def parse_duration(m1: dict[int, list], field_num: int) -> float | None:
    if field_num not in m1:
        return None
    d = parse_pb(m1[field_num][0])
    return d.get(1, [0])[0] + d.get(2, [0])[0] / 1e9


def parse_gen_row(idx: int, data: bytes) -> dict | None:
    if not data:
        return None
    top = parse_pb(data)
    if 1 not in top:
        return None
    m1 = parse_pb(top[1][0])
    usage = parse_pb(m1[4][0]) if 4 in m1 else {}
    uncached_in = usage.get(2, [0])[0]
    cached_in = usage.get(5, [0])[0]
    total_out = usage.get(3, [0])[0]
    ttft = parse_duration(m1, 11)
    streaming = parse_duration(m1, 12)

    max_ctx = 0
    if 9 in m1:
        m9 = parse_pb(m1[9][0])
        if 10 in m9:
            m10 = parse_pb(m9[10][0])
            max_ctx = m10.get(4, [0])[0]

    last_step_index = -1
    if 20 in m1:
        for kv_buf in m1[20]:
            kv = parse_pb(kv_buf)
            key = kv.get(1, [b""])[0].decode("utf-8", errors="ignore")
            if key == "last_step_index" and 2 in kv:
                try:
                    last_step_index = int(kv[2][0].decode("utf-8", errors="ignore"))
                except ValueError:
                    pass
                break

    total_ctx = uncached_in + cached_in
    tps = (total_out / streaming) if (total_out > 0 and streaming and streaming > 0) else None
    has_usage = 4 in m1 and (3 in usage or (ttft is not None and streaming is not None))
    if not has_usage:
        return None

    model_name = ""
    if 21 in m1 and m1[21]:
        model_name = m1[21][0].decode("utf-8", errors="ignore").strip()
    elif 19 in m1 and m1[19]:
        model_name = m1[19][0].decode("utf-8", errors="ignore").strip()

    return {
        "idx": idx,
        "lastStepIndex": last_step_index,
        "model": model_name,
        "uncachedIn": uncached_in,
        "cachedIn": cached_in,
        "contextTokens": total_ctx,
        "maxContextTokens": max_ctx,
        "totalOut": total_out,
        "ttft": ttft,
        "streaming": streaming,
        "tps": tps,
    }


def parse_step_time(meta_blob: bytes | None) -> str:
    if not meta_blob:
        return "—"
    meta = parse_pb(meta_blob)
    if 1 in meta and meta[1]:
        ts = parse_pb(meta[1][0])
        sec = ts.get(1, [None])[0]
        if isinstance(sec, int) and sec > 1_000_000_000:
            return datetime.fromtimestamp(sec).strftime("%m/%d %H:%M")
    return "—"


def group_by_turns(requests: list[dict], user_steps: list[tuple[int, str]]) -> list[dict]:
    if not user_steps:
        ttft_sum = sum(r["ttft"] or 0 for r in requests)
        stream_sum = sum(r["streaming"] or 0 for r in requests)
        cached_in = sum(r["cachedIn"] for r in requests)
        uncached_in = sum(r["uncachedIn"] for r in requests)
        total_out = sum(r["totalOut"] for r in requests)
        return [
            {
                "turnNo": 1,
                "requestCount": len(requests),
                "startReq": 1,
                "endReq": len(requests),
                "durSum": ttft_sum + stream_sum,
                "cachedIn": cached_in,
                "totalIn": cached_in + uncached_in,
                "totalOut": total_out,
                "tps": (total_out / stream_sum) if (total_out > 0 and stream_sum > 0) else None,
                "time": "—",
            }
        ]

    buckets: dict[int, list[dict]] = {i: [] for i in range(len(user_steps))}
    for r in requests:
        ls = r["lastStepIndex"]
        if ls < 0:
            turn_idx = len(user_steps) - 1
        else:
            turn_idx = 0
            for i, (step_idx, _) in enumerate(user_steps):
                if ls >= step_idx:
                    turn_idx = i
                else:
                    break
        buckets[turn_idx].append(r)

    turns = []
    cum_reqs = 0
    for i in range(len(user_steps)):
        reqs = buckets[i]
        if not reqs:
            continue
        start_req = cum_reqs + 1
        cum_reqs += len(reqs)
        ttft_sum = sum(r["ttft"] or 0 for r in reqs)
        stream_sum = sum(r["streaming"] or 0 for r in reqs)
        cached_in = sum(r["cachedIn"] for r in reqs)
        uncached_in = sum(r["uncachedIn"] for r in reqs)
        total_out = sum(r["totalOut"] for r in reqs)
        turns.append({
            "turnNo": i + 1,
            "requestCount": len(reqs),
            "startReq": start_req,
            "endReq": cum_reqs,
            "durSum": ttft_sum + stream_sum,
            "cachedIn": cached_in,
            "totalIn": cached_in + uncached_in,
            "totalOut": total_out,
            "tps": (total_out / stream_sum) if (total_out > 0 and stream_sum > 0) else None,
            "time": user_steps[i][1],
        })
    return turns[::-1]


_CONV_CACHE_LOCK = threading.Lock()
_CONV_CACHE: dict[str, tuple[int, int, dict]] = {}


def is_valid_cid(cid: str) -> bool:
    return bool(cid) and all(c.isalnum() or c in "-_" for c in cid)


def inspect_conversation(cid: str) -> dict | None:
    if not is_valid_cid(cid):
        return None
    db_path = BASE_DIR / "conversations" / f"{cid}.db"
    if not db_path.is_file():
        return None
    try:
        db_mtime = db_path.stat().st_mtime_ns
        wal_path = db_path.with_name(db_path.name + "-wal")
        wal_mtime = wal_path.stat().st_mtime_ns if wal_path.is_file() else 0
    except OSError:
        return None

    with _CONV_CACHE_LOCK:
        cached = _CONV_CACHE.get(cid)
        if cached and cached[0] == db_mtime and cached[1] == wal_mtime:
            return cached[2]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC").fetchall()
        user_steps = [
            (r[0], parse_step_time(r[1]))
            for r in conn.execute("SELECT idx, metadata FROM steps WHERE step_type = 14 ORDER BY idx ASC").fetchall()
        ]
    finally:
        conn.close()

    requests = []
    for idx, blob in rows:
        item = parse_gen_row(idx, blob)
        if item:
            requests.append(item)

    if not requests:
        result = {
            "id": cid,
            "latest": None,
            "totals": None,
            "turns": [],
            "mtimeMs": int(db_mtime / 1_000_000),
        }
        with _CONV_CACHE_LOCK:
            _CONV_CACHE[cid] = (db_mtime, wal_mtime, result)
        return result

    latest = requests[-1]
    turns = group_by_turns(requests, user_steps)
    total_cached = sum(r["cachedIn"] for r in requests)
    total_uncached = sum(r["uncachedIn"] for r in requests)
    total_ttft = sum(r["ttft"] or 0.0 for r in requests)
    total_stream = sum(r["streaming"] or 0.0 for r in requests)
    total_out = sum(r["totalOut"] for r in requests)
    totals = {
        "requests": len(requests),
        "totalIn": total_cached + total_uncached,
        "cachedIn": total_cached,
        "uncachedIn": total_uncached,
        "totalOut": total_out,
        "durSum": total_ttft + total_stream,
        "tps": (total_out / total_stream) if (total_out > 0 and total_stream > 0) else None,
    }
    result = {
        "id": cid,
        "latest": latest,
        "totals": totals,
        "turns": turns,
        "mtimeMs": int(db_mtime / 1_000_000),
    }
    with _CONV_CACHE_LOCK:
        if len(_CONV_CACHE) > 128:
            _CONV_CACHE.clear()
        _CONV_CACHE[cid] = (db_mtime, wal_mtime, result)
    return result


def recent_conversation_ids(limit: int = 10) -> list[str]:
    sum_db = BASE_DIR / "conversation_summaries.db"
    if not sum_db.is_file():
        return []
    conn = sqlite3.connect(f"file:{sum_db}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            "SELECT conversation_id FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [cid for (cid,) in rows if (BASE_DIR / "conversations" / f"{cid}.db").is_file()]


def snapshot_signature() -> tuple:
    conv_dir = BASE_DIR / "conversations"
    newest = 0
    count = 0
    newest_cid = ""
    if conv_dir.is_dir():
        for p in conv_dir.iterdir():
            if p.suffix in (".db", ".db-wal"):
                count += 1
                try:
                    ns = p.stat().st_mtime_ns
                    if ns > newest:
                        newest = ns
                        newest_cid = p.name.split(".")[0]
                except OSError:
                    pass
    sum_db = BASE_DIR / "conversation_summaries.db"
    sum_ns = 0
    if sum_db.is_file():
        try:
            sum_ns = sum_db.stat().st_mtime_ns
        except OSError:
            pass
    return (count, newest, sum_ns, newest_cid)


# ---------------------------------------------------------------------------
# Direct OAuth Token Reader & v1internal Quota Tracker (Scheme D)
# ---------------------------------------------------------------------------

def _parse_iso_ts(val: str | None) -> float:
    if not val:
        return 0.0
    try:
        s = val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Trim sub-microsecond nanoseconds if present (e.g. .8819952+08:00)
        if "." in s:
            head, tail = s.split(".", 1)
            tz_pos = max(tail.find("+"), tail.find("-"))
            if tz_pos != -1:
                frac, tz = tail[:tz_pos][:6], tail[tz_pos:]
                s = f"{head}.{frac}{tz}"
            else:
                s = f"{head}.{tail[:6]}"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _read_windows_credential(target: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
            ]

        advapi32 = ctypes.windll.advapi32
        advapi32.CredReadW.argtypes = [wt.LPWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        advapi32.CredReadW.restype = wt.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]

        pcred = ctypes.POINTER(CREDENTIAL)()
        if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pcred)):
            return None
        try:
            raw = ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
            return raw.decode("utf-8", errors="ignore")
        finally:
            advapi32.CredFree(pcred)
    except Exception:
        return None


_AUTH_EMAIL_RE = re.compile(r"authenticated successfully as ([^\s@,;)\]]+@[^\s@,;)\]]+)")
_SEND_CID_RE = re.compile(r"SEND_USER_CASCADE_MESSAGE_LATENCY\b.*?cascade_id:([0-9a-fA-F-]{8,64})")
_LOGOUT_MARKER = "Logging out current user"
_CLI_LOG_CACHE: dict[str, tuple[int, int, dict[str, str], str]] = {}
_CLI_LOG_CACHE_LOCK = threading.Lock()


def _norm_email(val: str | None) -> str:
    return val.strip().lower() if isinstance(val, str) else ""


def _parse_cli_log_cached(log_file: Path) -> tuple[dict[str, str], str]:
    """Parse a single agy cli-*.log into ({cid_lower: last_request_email}, final_auth_email)."""
    try:
        st = log_file.stat()
    except OSError:
        return {}, ""
    key = str(log_file)
    with _CLI_LOG_CACHE_LOCK:
        cached = _CLI_LOG_CACHE.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2], cached[3]

    try:
        text = log_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}, ""

    conv_emails: dict[str, str] = {}
    cur_email = ""
    for line in text.splitlines():
        if _LOGOUT_MARKER in line:
            cur_email = ""
            continue
        m_auth = _AUTH_EMAIL_RE.search(line)
        if m_auth:
            cur_email = m_auth.group(1).strip().rstrip(".")
            continue
        if cur_email:
            m_cid = _SEND_CID_RE.search(line)
            if m_cid:
                conv_emails[m_cid.group(1).lower()] = cur_email

    with _CLI_LOG_CACHE_LOCK:
        if len(_CLI_LOG_CACHE) > 512:
            _CLI_LOG_CACHE.clear()
        _CLI_LOG_CACHE[key] = (st.st_mtime_ns, st.st_size, conv_emails, cur_email)
    return conv_emails, cur_email


def get_latest_cli_log_email(log_dir: Path | None = None, limit: int = 10) -> str:
    """Return the most recent authenticated account email seen in agy cli-*.log files."""
    directory = log_dir or (BASE_DIR / "log")
    if not directory.is_dir():
        return ""
    logs = sorted(directory.glob("cli-*.log"), key=lambda p: p.name, reverse=True)[:limit]
    for log_file in logs:
        _, final_email = _parse_cli_log_cached(log_file)
        if final_email:
            return final_email
    return ""


def get_conversation_last_account(cid: str, log_dir: Path | None = None) -> str:
    """Return the Google account email used for the latest user request of `cid` from agy cli-*.log."""
    target = (cid or "").strip().lower()
    if not target:
        return ""
    directory = log_dir or (BASE_DIR / "log")
    if not directory.is_dir():
        return ""
    logs = sorted(directory.glob("cli-*.log"), key=lambda p: p.name, reverse=True)
    for log_file in logs:
        conv_emails, _ = _parse_cli_log_cached(log_file)
        email = conv_emails.get(target)
        if email:
            return email
    return ""


def read_oauth_token(
    base_dir: Path | None = None,
    log_dir: Path | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return (access_token, expiry_iso, email) following agy ChainedAuth precedence.

    Prefers Windows Credential Manager (keyring) on desktop and falls back to
    `antigravity-oauth-token` (SSH). When both exist with different emails,
    disambiguates using the latest active cli-*.log auth event.
    """
    root = base_dir or BASE_DIR
    file_cand: tuple[float, str, str | None, str | None] | None = None
    cred_cand: tuple[float, str, str | None, str | None] | None = None

    token_file = root / "antigravity-oauth-token"
    if token_file.is_file():
        try:
            parsed = json.loads(token_file.read_text(encoding="utf-8", errors="ignore"))
            tok = (parsed.get("token") or {}).get("access_token")
            if tok:
                exp_s = (parsed.get("token") or {}).get("expiry")
                file_cand = (_parse_iso_ts(exp_s), tok, exp_s, parsed.get("id_token_email"))
        except Exception:
            pass

    if base_dir is None:
        raw_cred = _read_windows_credential("gemini:antigravity")
        if raw_cred:
            try:
                parsed = json.loads(raw_cred)
                tok = (parsed.get("token") or {}).get("access_token")
                if tok:
                    exp_s = (parsed.get("token") or {}).get("expiry")
                    cred_cand = (_parse_iso_ts(exp_s), tok, exp_s, parsed.get("id_token_email"))
            except Exception:
                pass

    if cred_cand and not file_cand:
        _, tok, exp_s, email = cred_cand
        return (tok, exp_s, email)
    if file_cand and not cred_cand:
        _, tok, exp_s, email = file_cand
        return (tok, exp_s, email)
    if not cred_cand and not file_cand:
        return (None, None, None)

    if _norm_email(cred_cand[3]) == _norm_email(file_cand[3]):
        best = file_cand if file_cand[0] > cred_cand[0] else cred_cand
        return (best[1], best[2], best[3])

    resolved_log_dir = log_dir if log_dir is not None else (root / "log")
    log_email = _norm_email(get_latest_cli_log_email(resolved_log_dir))
    if log_email and log_email == _norm_email(file_cand[3]):
        return (file_cand[1], file_cand[2], file_cand[3])
    return (cred_cand[1], cred_cand[2], cred_cand[3])


def find_agy_executable() -> str | None:
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe",
            Path.home() / ".gemini" / "bin" / "agy.exe",
        ]
        for p in candidates:
            if p.is_file():
                return str(p)
    else:
        p = Path.home() / ".gemini" / "bin" / "agy"
        if p.is_file():
            return str(p)
    return shutil.which("agy")


def trigger_agy_token_refresh(timeout: float = 12.0) -> bool:
    """Invoke `agy models` hidden to refresh the OAuth access_token via refresh_token."""
    exe = find_agy_executable()
    if not exe:
        return False
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        res = subprocess.run([exe, "models"], **kwargs)
        return res.returncode == 0
    except Exception:
        return False


def _win32_list_agy_pids() -> list[int]:
    """Return PIDs of all running agy.exe processes via Win32 Toolhelp32 snapshot."""
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wt.BOOL
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    pids: list[int] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == "agy.exe":
                    pids.append(pe.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    return pids


def model_to_bucket(model_name: str) -> str:
    low = (model_name or "").lower()
    if any(k in low for k in ("claude", "gpt", "opus", "sonnet")):
        return "3p-5h"
    return "gemini-5h"


class QuotaTracker:
    """Event-driven direct v1internal quota fetcher & token-to-quota delta correlator."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._cli_refresh_lock = threading.Lock()
        self._last_cli_refresh_ts: float = 0.0
        self._last_fetch_ts: float = 0.0
        self._last_token: str | None = None
        self._last_email: str | None = None
        self._error: str | None = None
        self._version: int = 0

        # Watermarks for correlating DB token deltas to quota drops:
        # conv_id -> { requests, uncachedIn, cachedIn, totalOut, model }
        self._conv_watermarks: dict[str, dict] = {}
        # bucket_key -> { fraction, resetTime, acc_reqs, acc_uncached, acc_cached, acc_out, last_cid, last_model }
        self._bucket_acc: dict[str, dict] = {}

        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_deltas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    conversation_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    bucket_key TEXT NOT NULL,
                    req_count INTEGER NOT NULL,
                    delta_reqs INTEGER NOT NULL,
                    delta_uncached_in INTEGER NOT NULL,
                    delta_cached_in INTEGER NOT NULL,
                    delta_think INTEGER NOT NULL,
                    delta_reply INTEGER NOT NULL,
                    delta_total_out INTEGER NOT NULL,
                    quota_before REAL NOT NULL,
                    quota_after REAL NOT NULL,
                    delta_fraction REAL NOT NULL,
                    reset_time TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qd_bucket_ts ON quota_deltas(bucket_key, ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qd_conv_ts ON quota_deltas(conversation_id, ts DESC)")
            conn.commit()
        finally:
            conn.close()

    def _post_v1(self, path: str, token: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{CLOUDCODE_BASE}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def record_conversation_activity(self, cid: str) -> bool:
        """Inspect a conversation's latest totals and accumulate token deltas since last check.
        Returns True if new requests were observed."""
        if not cid:
            return False
        info = inspect_conversation(cid)
        if not info or not info.get("totals") or not info.get("latest"):
            return False
        t = info["totals"]
        model = info["latest"].get("model", "")
        bkey = model_to_bucket(model)

        with self._lock:
            prev = self._conv_watermarks.get(cid)
            curr = {
                "requests": t["requests"],
                "uncachedIn": t["uncachedIn"],
                "cachedIn": t["cachedIn"],
                "totalOut": t["totalOut"],
                "model": model,
            }
            self._conv_watermarks[cid] = curr
            if prev is None:
                # First time seeing this conversation since server start: establish baseline
                return False

            d_reqs = curr["requests"] - prev["requests"]
            if d_reqs <= 0:
                return False

            acc = self._bucket_acc.setdefault(bkey, {
                "fraction": None,
                "resetTime": "",
                "acc_reqs": 0,
                "acc_uncached": 0,
                "acc_cached": 0,
                "acc_out": 0,
                "last_cid": cid,
                "last_model": model,
                "last_req_count": curr["requests"],
            })
            acc["acc_reqs"] += d_reqs
            acc["acc_uncached"] += max(0, curr["uncachedIn"] - prev["uncachedIn"])
            acc["acc_cached"] += max(0, curr["cachedIn"] - prev["cachedIn"])
            acc["acc_out"] += max(0, curr["totalOut"] - prev["totalOut"])
            acc["last_cid"] = cid
            acc["last_model"] = model
            acc["last_req_count"] = curr["requests"]
            return True

    def seed_watermarks(self) -> None:
        """Establish initial token baselines for recent conversations so the very next turn produces a clean delta."""
        try:
            for cid in recent_conversation_ids(10):
                self.record_conversation_activity(cid)
        except Exception:
            pass

    def has_new_token_while_errored(self) -> bool:
        with self._lock:
            if not self._error:
                return False
            prev_tok = self._last_token
        tok, _, _ = read_oauth_token()
        return bool(tok and tok != prev_tok)

    def has_auth_change(self) -> bool:
        with self._lock:
            prev_tok = self._last_token
            prev_email = _norm_email(self._last_email)
            errored = bool(self._error)
        tok, _, email = read_oauth_token()
        curr_email = _norm_email(email)
        if curr_email and prev_email and curr_email != prev_email:
            return True
        if errored and tok and tok != prev_tok:
            return True
        return False

    def _fetch_buckets(self, token: str) -> dict[str, dict]:
        buckets: dict[str, dict] = {}
        # 1. Fetch per-model quotaInfo (7-digit float32 precision for Gemini & 3P models)
        fam = self._post_v1("/v1internal:fetchAvailableModels", token, {})
        models_map = fam.get("models") or {}

        gemini_qi = None
        third_party_qi = None
        for mid, minfo in models_map.items():
            qi = minfo.get("quotaInfo")
            if not qi or "remainingFraction" not in qi:
                continue
            low = mid.lower()
            if low.startswith("gemini-") and gemini_qi is None:
                gemini_qi = qi
            elif (low.startswith("claude-") or low.startswith("gpt-")) and third_party_qi is None:
                third_party_qi = qi

        if gemini_qi is not None:
            buckets["gemini-5h"] = {
                "bucketId": "gemini-5h",
                "remainingFraction": float(gemini_qi["remainingFraction"]),
                "resetTime": gemini_qi.get("resetTime", ""),
            }
        if third_party_qi is not None:
            buckets["3p-5h"] = {
                "bucketId": "3p-5h",
                "remainingFraction": float(third_party_qi["remainingFraction"]),
                "resetTime": third_party_qi.get("resetTime", ""),
            }
        return buckets

    def _try_cli_refresh_token(self, stale_token: str) -> tuple[str | None, str | None, str | None]:
        """Auto-refresh expired OAuth token via `agy models` (rate-limited to once per 60s)."""
        with self._cli_refresh_lock:
            # Check if another thread or agy already refreshed the token
            tok, exp, email = read_oauth_token()
            if tok and tok != stale_token:
                return (tok, exp, email)
            now = time.time()
            if now - self._last_cli_refresh_ts < 60.0:
                return (None, None, None)
            self._last_cli_refresh_ts = now
            if not trigger_agy_token_refresh():
                return (None, None, None)
            tok, exp, email = read_oauth_token()
            if tok and tok != stale_token:
                return (tok, exp, email)
            return (None, None, None)

    def refresh(self, force: bool = False, min_interval: float = 10.0) -> bool:
        now = time.time()
        token, expiry, email = read_oauth_token()
        with self._lock:
            token_changed = bool(token and token != self._last_token)
            curr_email = _norm_email(email)
            prev_email = _norm_email(self._last_email)
            email_changed = bool(curr_email and prev_email and curr_email != prev_email)
            cooldown = 4.0 if force else min_interval
            if not token_changed and not email_changed and (now - self._last_fetch_ts < cooldown):
                return False
            self._last_fetch_ts = now
            self._last_token = token

        if not token:
            with self._lock:
                self._error = "找不到 OAuth Token（請先啟動 agy 登入）"
            return False

        buckets: dict[str, dict] = {}
        err_msg: str | None = None

        try:
            buckets = self._fetch_buckets(token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                new_tok, new_exp, new_email = self._try_cli_refresh_token(token)
                if new_tok:
                    token, expiry, email = new_tok, new_exp, new_email
                    with self._lock:
                        self._last_token = token
                    try:
                        buckets = self._fetch_buckets(token)
                    except urllib.error.HTTPError as e2:
                        err_msg = f"Quota API HTTP {e2.code}"
                    except Exception as e2:
                        err_msg = f"Quota API 錯誤: {e2}"
                else:
                    err_msg = "Token 已過期（自動刷新失敗，請重新登入 agy）"
            else:
                err_msg = f"Quota API HTTP {e.code}"
        except Exception as e:
            err_msg = f"Quota API 錯誤: {e}"

        if err_msg:
            with self._lock:
                self._error = err_msg
            return False

        # Correlate quota drops with accumulated token deltas
        new_rows: list[tuple] = []
        ts_now = time.time()
        with self._lock:
            self._error = None
            self._version += 1
            curr_email = _norm_email(email)
            prev_email = _norm_email(self._last_email)
            account_switched = bool(curr_email and prev_email and curr_email != prev_email)
            if curr_email:
                self._last_email = curr_email
            if account_switched:
                # Reset all bucket baselines on account switch to avoid bogus cross-account deltas
                self._bucket_acc = {
                    bkey: {
                        "fraction": binfo["remainingFraction"],
                        "resetTime": binfo.get("resetTime", ""),
                        "acc_reqs": 0,
                        "acc_uncached": 0,
                        "acc_cached": 0,
                        "acc_out": 0,
                        "last_cid": "",
                        "last_model": "",
                        "last_req_count": 0,
                    }
                    for bkey, binfo in buckets.items()
                }
                return True

            for bkey, binfo in buckets.items():
                curr_frac = binfo["remainingFraction"]
                curr_reset = binfo.get("resetTime", "")
                acc = self._bucket_acc.get(bkey)
                if acc is None:
                    self._bucket_acc[bkey] = {
                        "fraction": curr_frac,
                        "resetTime": curr_reset,
                        "acc_reqs": 0,
                        "acc_uncached": 0,
                        "acc_cached": 0,
                        "acc_out": 0,
                        "last_cid": "",
                        "last_model": "",
                        "last_req_count": 0,
                    }
                    continue

                prev_frac = acc["fraction"]
                prev_reset = acc["resetTime"]

                # Detect quota window reset or upward jump -> reset baseline without recording bogus delta
                if prev_frac is None or curr_frac > prev_frac + 1e-6 or (prev_reset and curr_reset and prev_reset != curr_reset and curr_frac >= prev_frac):
                    acc["fraction"] = curr_frac
                    acc["resetTime"] = curr_reset
                    acc["acc_reqs"] = 0
                    acc["acc_uncached"] = 0
                    acc["acc_cached"] = 0
                    acc["acc_out"] = 0
                    continue

                delta_frac = prev_frac - curr_frac
                if delta_frac > 1e-7 and acc["acc_reqs"] > 0:
                    new_rows.append((
                        ts_now,
                        acc["last_cid"],
                        acc["last_model"],
                        bkey,
                        acc["last_req_count"],
                        acc["acc_reqs"],
                        acc["acc_uncached"],
                        acc["acc_cached"],
                        0,
                        0,
                        acc["acc_out"],
                        prev_frac,
                        curr_frac,
                        delta_frac,
                        curr_reset,
                    ))
                    acc["fraction"] = curr_frac
                    acc["resetTime"] = curr_reset
                    acc["acc_reqs"] = 0
                    acc["acc_uncached"] = 0
                    acc["acc_cached"] = 0
                    acc["acc_out"] = 0
                elif delta_frac > 1e-7:
                    # Quota dropped from another client/session; update baseline so future deltas stay clean
                    acc["fraction"] = curr_frac
                    acc["resetTime"] = curr_reset

        if new_rows:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.executemany(
                    """
                    INSERT INTO quota_deltas (
                        ts, conversation_id, model, bucket_key, req_count, delta_reqs,
                        delta_uncached_in, delta_cached_in, delta_think, delta_reply, delta_total_out,
                        quota_before, quota_after, delta_fraction, reset_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    new_rows,
                )
                conn.commit()
            finally:
                conn.close()
        return True

    def _query_conv_deltas(self, cid: str | None) -> list[dict]:
        if not cid:
            return []
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            conv_rows = conn.execute(
                """
                SELECT bucket_key, req_count, delta_fraction
                FROM quota_deltas
                WHERE conversation_id = ?
                ORDER BY ts DESC
                """,
                (cid,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "bucketKey": r[0],
                "reqCount": r[1],
                "deltaPct": round(r[2] * 100.0, 4),
            }
            for r in conv_rows
        ]

    def _filter_5h_deltas(self, conv_deltas: list[dict], active_bucket: str | None) -> tuple[list[dict], float | None]:
        if active_bucket:
            deltas_5h = [d for d in conv_deltas if d.get("bucketKey") == active_bucket]
        else:
            deltas_5h = [d for d in conv_deltas if str(d.get("bucketKey", "")).endswith("-5h")]
        total_pct = round(sum(d["deltaPct"] for d in deltas_5h), 4) if deltas_5h else None
        return deltas_5h, total_pct

    def get_state(self, cid: str | None = None, active_bucket: str | None = None) -> dict:
        with self._lock:
            meta = {
                "lastUpdatedMs": int(self._last_fetch_ts * 1000) if self._last_fetch_ts else 0,
                "error": self._error,
                "version": self._version,
            }
        conv_deltas = self._query_conv_deltas(cid)
        deltas_5h, conv_total_delta_pct = self._filter_5h_deltas(conv_deltas, active_bucket)
        return {
            **meta,
            "convDeltas": deltas_5h,
            "convTotalDeltaPct": conv_total_delta_pct,
        }

    def version(self) -> int:
        with self._lock:
            return self._version


QUOTA_TRACKER = QuotaTracker(QUOTA_DB_PATH)


def start_quota_watcher() -> None:
    """Background event-driven watcher: refreshes quota when conversations/*.db or OAuth token changes."""
    def _loop():
        QUOTA_TRACKER.seed_watermarks()
        QUOTA_TRACKER.refresh(force=True)
        last_sig = snapshot_signature()
        pending_refresh_at: float = 0.0
        tick = 0
        while True:
            time.sleep(0.8)
            tick += 1
            sig = snapshot_signature()
            if sig[:3] != last_sig[:3]:
                last_sig = sig
                cid = sig[3]
                if cid and QUOTA_TRACKER.record_conversation_activity(cid):
                    # Debounce 1.5s to coalesce rapid tool-call bursts
                    pending_refresh_at = time.time() + 1.5
            if pending_refresh_at and time.time() >= pending_refresh_at:
                pending_refresh_at = 0.0
                QUOTA_TRACKER.refresh(force=False, min_interval=10.0)
            elif tick % 3 == 0 and QUOTA_TRACKER.has_auth_change():
                QUOTA_TRACKER.refresh(force=True)

    threading.Thread(target=_loop, daemon=True).start()


def attach_turn_quotas(metrics: dict | None, conv_deltas: list[dict]) -> dict | None:
    """Return a shallow copy of metrics with per-conversation 5h quota deltas merged into each turn."""
    if not metrics or not metrics.get("turns") or not conv_deltas:
        return metrics
    turns_out = []
    for turn in metrics["turns"]:
        s_req = turn.get("startReq", 0)
        e_req = turn.get("endReq", 0)
        matched = [d["deltaPct"] for d in conv_deltas if s_req and e_req and s_req <= d.get("reqCount", 0) <= e_req]
        if matched:
            turns_out.append({**turn, "quotaDeltaPct": round(sum(matched), 4)})
        else:
            turns_out.append(turn)
    return {**metrics, "turns": turns_out}


def get_active_payload(requested_cid: str = "", force_refresh: bool = False) -> dict:
    if force_refresh:
        QUOTA_TRACKER.refresh(force=True)

    cid = requested_cid.strip().lower()
    conv_meta = None
    if is_valid_cid(cid):
        conv_meta = {"id": cid}
        if not (BASE_DIR / "conversations" / f"{cid}.db").is_file():
            cid = ""
    else:
        cid = ""

    metrics = inspect_conversation(cid) if cid else None
    has_latest = bool(metrics and metrics.get("latest"))
    model_name = metrics["latest"].get("model", "") if has_latest else ""
    account_email = get_conversation_last_account(cid) if (cid and has_latest) else ""
    active_bucket = model_to_bucket(model_name)
    quota = QUOTA_TRACKER.get_state(cid=cid or None, active_bucket=active_bucket)
    metrics = attach_turn_quotas(metrics, quota.get("convDeltas") or [])

    return {
        "conversation": conv_meta,
        "metrics": metrics,
        "accountEmail": account_email,
        "activeBucket": active_bucket,
        "quota": quota,
    }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            info = {"service": "agy-metrics", "status": "ok"}
            self._send(200, "application/json; charset=utf-8", json.dumps(info, ensure_ascii=False).encode("utf-8"))
        elif parsed.path == "/api/active":
            cid = qs.get("id", [""])[0]
            force = qs.get("refresh", ["0"])[0] in ("1", "true")
            data = get_active_payload(requested_cid=cid, force_refresh=force)
            self._send(200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))
        elif parsed.path == "/api/ping":
            sig = snapshot_signature()[:3]
            token = f"{sig[0]}:{sig[1]}:{sig[2]}:{QUOTA_TRACKER.version()}"
            self._send(200, "application/json; charset=utf-8", json.dumps({"token": token}).encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not Found")

    def do_POST(self):
        if urlparse(self.path).path == "/api/shutdown":
            client_ip = (self.client_address[0] if self.client_address else "")
            if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._send(403, "application/json; charset=utf-8", b'{"error":"loopback only"}')
                return
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not Found")


def is_agy_running() -> bool:
    try:
        if os.name == "nt":
            return bool(_win32_list_agy_pids())
        res = subprocess.run(["pgrep", "-x", "agy"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return res.returncode == 0
    except Exception:
        return True


def watch_agy_exit(server: ThreadingHTTPServer) -> None:
    started_at = time.time()
    saw_agy = False
    while True:
        time.sleep(1)
        if is_agy_running():
            saw_agy = True
        elif saw_agy or (time.time() - started_at > 10):
            server.shutdown()
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    parser.add_argument("--port", type=int, default=47891)
    args = parser.parse_args()
    host = "0.0.0.0" if args.lan else args.host
    start_quota_watcher()
    server = ThreadingHTTPServer((host, args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=watch_agy_exit, args=(server,), daemon=True).start()
    print(f"[agy-metrics] listening on http://{host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
