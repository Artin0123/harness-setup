#!/usr/bin/env python3
"""Sidecar & standalone web UI for Antigravity CLI / Remote Control telemetry."""

import argparse
import json
import os
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


def resolve_data_dir() -> Path:
    env_dir = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
    if env_dir:
        p = Path(env_dir).resolve()
        for parent in [p, *p.parents]:
            if parent.name == "sidecar_data":
                return parent.parent
        return p
    return Path.home() / ".gemini" / "antigravity-cli"


BASE_DIR = resolve_data_dir()
INDEX_HTML = Path(__file__).with_name("index.html")


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
        wt = tag & 7
        if fn == 0:
            break
        fields.setdefault(fn, [])
        if wt == 0:
            v, i = read_varint(buf, i)
            fields[fn].append(v)
        elif wt == 2:
            l, i = read_varint(buf, i)
            if i + l > len(buf):
                break
            fields[fn].append(buf[i : i + l])
            i += l
        elif wt == 5:
            fields[fn].append(buf[i : i + 4])
            i += 4
        elif wt == 1:
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
    think = usage.get(9, [0])[0]
    reply = usage.get(10, [0])[0]
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

    return {
        "idx": idx,
        "lastStepIndex": last_step_index,
        "uncachedIn": uncached_in,
        "cachedIn": cached_in,
        "contextTokens": total_ctx,
        "maxContextTokens": max_ctx,
        "think": think,
        "reply": reply,
        "totalOut": total_out,
        "ttft": ttft,
        "streaming": streaming,
        "tps": tps,
    }


def classify_group(project_id: str, workspace_uris: str) -> tuple[str, str, str]:
    """Return (filter_category, badge_label, badge_kind)."""
    ws_name = ""
    if workspace_uris:
        try:
            uris = json.loads(workspace_uris)
            if isinstance(uris, list) and uris:
                raw_path = unquote(str(uris[0]))
                ws_name = Path(raw_path.replace("file:///", "")).name
        except Exception:
            pass
    if project_id == "default-cli-project":
        return ("專案", "CLI Project", "cli")
    if project_id and project_id != "outside-of-project":
        return ("專案", ws_name or project_id[:8], "proj")
    return ("一般", "", "gen")


def group_by_turns(requests: list[dict], user_steps: list[int]) -> list[dict]:
    if not user_steps:
        ttft_sum = sum(r["ttft"] or 0 for r in requests)
        stream_sum = sum(r["streaming"] or 0 for r in requests)
        total_out = sum(r["totalOut"] for r in requests)
        return [
            {
                "turnNo": 1,
                "requestCount": len(requests),
                "ttftSum": ttft_sum,
                "durSum": ttft_sum + stream_sum,
                "think": sum(r["think"] for r in requests),
                "reply": sum(r["reply"] for r in requests),
                "totalOut": total_out,
                "tps": (total_out / stream_sum) if (total_out > 0 and stream_sum > 0) else None,
            }
        ]

    buckets: dict[int, list[dict]] = {i: [] for i in range(len(user_steps))}
    for r in requests:
        ls = r["lastStepIndex"]
        if ls < 0:
            turn_idx = len(user_steps) - 1
        else:
            turn_idx = 0
            for i, step_idx in enumerate(user_steps):
                if ls >= step_idx:
                    turn_idx = i
                else:
                    break
        buckets[turn_idx].append(r)

    turns = []
    for i in range(len(user_steps)):
        reqs = buckets[i]
        if not reqs:
            continue
        ttft_sum = sum(r["ttft"] or 0 for r in reqs)
        stream_sum = sum(r["streaming"] or 0 for r in reqs)
        total_out = sum(r["totalOut"] for r in reqs)
        turns.append({
            "turnNo": i + 1,
            "requestCount": len(reqs),
            "ttftSum": ttft_sum,
            "durSum": ttft_sum + stream_sum,
            "think": sum(r["think"] for r in reqs),
            "reply": sum(r["reply"] for r in reqs),
            "totalOut": total_out,
            "tps": (total_out / stream_sum) if (total_out > 0 and stream_sum > 0) else None,
        })
    return turns[-30:][::-1]


def inspect_conversation(cid: str) -> dict | None:
    if not cid or not all(c.isalnum() or c in "-_" for c in cid):
        return None
    db_path = BASE_DIR / "conversations" / f"{cid}.db"
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC").fetchall()
        user_steps = [
            r[0]
            for r in conn.execute("SELECT idx FROM steps WHERE step_type = 14 ORDER BY idx ASC").fetchall()
        ]
    finally:
        conn.close()

    requests = []
    for idx, blob in rows:
        item = parse_gen_row(idx, blob)
        if item:
            requests.append(item)

    if not requests:
        return {
            "id": cid,
            "latest": None,
            "totals": None,
            "turns": [],
            "mtimeMs": int(db_path.stat().st_mtime * 1000),
        }

    latest = requests[-1]
    turns = group_by_turns(requests, user_steps)
    total_cached = sum(r["cachedIn"] for r in requests)
    total_uncached = sum(r["uncachedIn"] for r in requests)
    totals = {
        "requests": len(requests),
        "totalIn": total_cached + total_uncached,
        "cachedIn": total_cached,
        "uncachedIn": total_uncached,
        "think": sum(r["think"] for r in requests),
        "reply": sum(r["reply"] for r in requests),
        "totalOut": sum(r["totalOut"] for r in requests),
    }
    return {
        "id": cid,
        "latest": latest,
        "totals": totals,
        "turns": turns,
        "mtimeMs": int(db_path.stat().st_mtime * 1000),
    }


def list_conversations(query: str = "", group_filter: str = "all") -> list[dict]:
    sum_db = BASE_DIR / "conversation_summaries.db"
    if not sum_db.is_file():
        return []
    conn = sqlite3.connect(f"file:{sum_db}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            "SELECT conversation_id, title, last_modified_time, step_count, project_id, workspace_uris "
            "FROM conversation_summaries ORDER BY last_modified_time DESC"
        ).fetchall()
    finally:
        conn.close()

    q = query.strip().lower()
    out = []
    for cid, title, mtime_str, steps, proj_id, ws_uris in rows:
        db_path = BASE_DIR / "conversations" / f"{cid}.db"
        if not db_path.is_file():
            continue

        grp, badge_label, badge_kind = classify_group(proj_id or "", ws_uris or "")
        if group_filter == "project" and grp != "專案":
            continue
        if group_filter == "general" and grp != "一般":
            continue

        clean_title = (title or "(無標題)").strip()
        if q:
            hay = f"{cid} {clean_title} {badge_label}".lower()
            if q not in hay:
                continue

        out.append({
            "id": cid,
            "title": clean_title,
            "group": grp,
            "badgeLabel": badge_label,
            "badgeKind": badge_kind,
            "lastModified": mtime_str,
            "steps": steps,
        })
    return out


def snapshot_signature() -> tuple:
    conv_dir = BASE_DIR / "conversations"
    newest = 0
    count = 0
    if conv_dir.is_dir():
        for p in conv_dir.iterdir():
            if p.suffix in (".db", ".db-wal"):
                count += 1
                try:
                    ns = p.stat().st_mtime_ns
                    if ns > newest:
                        newest = ns
                except OSError:
                    pass
    sum_db = BASE_DIR / "conversation_summaries.db"
    sum_ns = 0
    if sum_db.is_file():
        try:
            sum_ns = sum_db.stat().st_mtime_ns
        except OSError:
            pass
    return (count, newest, sum_ns)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.read_bytes())
        elif parsed.path == "/api/conversations":
            q = qs.get("q", [""])[0]
            g = qs.get("group", ["all"])[0]
            data = list_conversations(query=q, group_filter=g)
            self._send(200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))
        elif parsed.path == "/api/metrics":
            cid = qs.get("id", [""])[0]
            data = inspect_conversation(cid)
            if not data:
                self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')
            else:
                self._send(200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))
        elif parsed.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last_sig = snapshot_signature()
            try:
                while True:
                    time.sleep(0.4)
                    sig = snapshot_signature()
                    if sig != last_sig:
                        last_sig = sig
                        self.wfile.write(b"event: update\ndata: 1\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not Found")

    def do_POST(self):
        if urlparse(self.path).path == "/api/shutdown":
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not Found")


def is_agy_running() -> bool:
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq agy.exe", "/NH"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True,
                errors="ignore",
                timeout=3,
            )
            return "agy.exe" in out.lower()
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
    sidecar_port = os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT")
    parser.add_argument("--port", type=int, default=int(sidecar_port or "47891"))
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if not sidecar_port:
        threading.Thread(target=watch_agy_exit, args=(server,), daemon=True).start()
    print(f"[agy-metrics] listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
