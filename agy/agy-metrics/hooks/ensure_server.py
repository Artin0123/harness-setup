#!/usr/bin/env python3
"""Ensure agy-metrics backend server is running in background on 127.0.0.1:47891."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 47891
BASE_URL = "https://antigravity.google.com"
REMOTE_PATH = "/remote?path=%2Fview%2Fremote-control"
REMOTE_URL = f"{BASE_URL}{REMOTE_PATH}"
_CID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUTH_EMAIL_RE = re.compile(r"authenticated successfully as ([^\s@,;)\]]+@[^\s@,;)\]]+)")
_LOGOUT_MARKER = "Logging out current user"
_REMOTE_CONNECTED_RE = re.compile(
    r"\[remote-control-([0-9a-fA-F-]+-v2)\] Connection status: Connected"
)
_LOG_PATH = Path.home() / ".gemini" / "antigravity-cli" / "plugin_data" / "agy-metrics" / "ensure_server.log"
_LOG_MAX_BYTES = 256 * 1024


def _log(msg: str, log_path: Path | None = None) -> None:
    target = log_path or _LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size > _LOG_MAX_BYTES:
            backup = target.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            target.replace(backup)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with target.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] [pid={os.getpid()}] {msg}\n")
    except OSError:
        pass


def is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_server(port: int) -> None:
    if not is_listening(port):
        return
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def spawn_detached(script_args: list[str]) -> None:
    exe = sys.executable
    if os.name == "nt":
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.is_file():
            exe = str(pythonw)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [exe, *script_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [exe, *script_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )


def _has_valid_project(pid: str, projects_dir: Path) -> bool:
    return bool(pid and _PID_RE.fullmatch(pid) and (projects_dir / f"{pid}.json").is_file())


def get_latest_conversation(
    db_path: Path | None = None,
    projects_dir: Path | None = None,
) -> tuple[str, str]:
    """Return (conversation_id, project_id) of the latest top-level conversation with a valid local project.

    Falls back to (latest_cid, '') when no top-level conversation has a valid local project config.
    """
    import sqlite3

    path = db_path or (Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")
    if not path.is_file():
        return "", ""
    proj_dir = projects_dir or (Path.home() / ".gemini" / "config" / "projects")
    con = None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        fallback_cid = ""
        for row in con.execute(
            "SELECT conversation_id, project_id FROM conversation_summaries "
            "WHERE length(parent_conversation_id) = 0 "
            "ORDER BY last_modified_time DESC"
        ):
            cid = str(row[0]) if row and row[0] else ""
            if not _CID_RE.fullmatch(cid):
                continue
            if not fallback_cid:
                fallback_cid = cid
            pid = str(row[1]) if len(row) > 1 and row[1] else ""
            if _has_valid_project(pid, proj_dir):
                return cid, pid
        return fallback_cid, ""
    except Exception as exc:
        _log(f"get_latest_conversation error: {exc!r} (db={path})")
        return "", ""
    finally:
        if con is not None:
            con.close()


def _read_windows_credential(target: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

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


def _extract_email(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return ""
        email = parsed.get("id_token_email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    except Exception:
        pass
    return ""


def _extract_last_auth_email(text: str) -> str:
    email = ""
    for line in text.splitlines():
        if _LOGOUT_MARKER in line:
            email = ""
            continue
        m = _AUTH_EMAIL_RE.search(line)
        if m:
            email = m.group(1).strip().rstrip(".")
    return email


def _get_latest_cli_log_email(log_dir: Path | None = None, limit: int = 10) -> str:
    directory = log_dir or (Path.home() / ".gemini" / "antigravity-cli" / "log")
    if not directory.is_dir():
        return ""
    logs = sorted(directory.glob("cli-*.log"), key=lambda p: p.name, reverse=True)[:limit]
    for log_file in logs:
        try:
            email = _extract_last_auth_email(log_file.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if email:
            return email
    return ""


def get_active_user_email(base_dir: Path | None = None, log_dir: Path | None = None) -> str:
    """Return the active agy Google account email following agy ChainedAuth precedence.

    Prefers Windows Credential Manager (keyring) on desktop sessions and falls back to
    `antigravity-oauth-token` (used in SSH sessions where Credential Manager is isolated).
    When both exist with different emails, disambiguates using the latest cli-*.log auth event.
    """
    root = base_dir or (Path.home() / ".gemini" / "antigravity-cli")

    keyring_email = ""
    if base_dir is None:
        raw_cred = _read_windows_credential("gemini:antigravity")
        if raw_cred:
            keyring_email = _extract_email(raw_cred)

    file_email = ""
    token_file = root / "antigravity-oauth-token"
    if token_file.is_file():
        try:
            file_email = _extract_email(token_file.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass

    if keyring_email and not file_email:
        return keyring_email
    if file_email and not keyring_email:
        return file_email
    if not keyring_email and not file_email:
        return ""

    if keyring_email.lower() == file_email.lower():
        return keyring_email

    resolved_log_dir = log_dir if log_dir is not None else (root / "log")
    log_email = _get_latest_cli_log_email(resolved_log_dir).lower()
    if log_email and log_email == file_email.lower():
        return file_email
    return keyring_email


def build_remote_url(
    session_id: str,
    cid: str = "",
    project_id: str = "",
    authuser: str = "",
) -> str:
    auth_suffix = (
        f"&authuser={urllib.parse.quote(authuser, safe='')}"
        if authuser
        else ""
    )
    if not cid:
        return f"{REMOTE_URL}{auth_suffix}"
    inner = f"c/{cid}"
    if project_id:
        inner = f"{inner}?project={project_id}"
    return f"{BASE_URL}/r/{session_id}?p={urllib.parse.quote(inner, safe='')}{auth_suffix}"


def wait_and_open_remote(
    after_log: str = "",
    cid: str = "",
    project_id: str = "",
    authuser: str = "",
    log_dir: Path | None = None,
    timeout_sec: float = 20.0,
) -> None:
    import webbrowser

    directory = log_dir or (Path.home() / ".gemini" / "antigravity-cli" / "log")
    _log(
        f"wait_and_open_remote start: after_log={after_log!r} cid={cid!r} "
        f"project_id={project_id!r} authuser={authuser!r} "
        f"log_dir={directory} timeout={timeout_sec}s"
    )
    deadline = time.monotonic() + timeout_sec
    seen_candidates: set[str] = set()
    while time.monotonic() < deadline:
        if directory.is_dir():
            candidates = sorted(
                (p for p in directory.glob("cli-*.log") if p.name > after_log),
                key=lambda p: p.name,
                reverse=True,
            )
            for log_file in candidates:
                if log_file.name not in seen_candidates:
                    seen_candidates.add(log_file.name)
                    _log(f"wait_and_open_remote: watching candidate {log_file.name}")
                try:
                    text = log_file.read_text(encoding="utf-8", errors="ignore")
                except OSError as exc:
                    _log(f"wait_and_open_remote: read failed {log_file.name}: {exc!r}")
                    continue
                m = _REMOTE_CONNECTED_RE.search(text)
                if m:
                    log_authuser = _extract_last_auth_email(text)
                    effective_authuser = log_authuser or authuser
                    url = build_remote_url(
                        m.group(1),
                        cid,
                        project_id,
                        authuser=effective_authuser,
                    )
                    try:
                        opened = webbrowser.open(url)
                        _log(
                            f"wait_and_open_remote: matched session={m.group(1)} "
                            f"in {log_file.name}, authuser={effective_authuser!r} "
                            f"(log={log_authuser!r} fallback={authuser!r}), "
                            f"webbrowser.open={opened} url={url}"
                        )
                    except Exception as exc:
                        _log(
                            f"wait_and_open_remote: webbrowser.open raised {exc!r} "
                            f"for url={url}"
                        )
                    return
        time.sleep(0.25)

    latest = []
    if directory.is_dir():
        latest = [
            f"{p.name}({p.stat().st_size}B)"
            for p in sorted(directory.glob("cli-*.log"), key=lambda p: p.name, reverse=True)[:3]
        ]
    _log(
        f"wait_and_open_remote: timeout after {timeout_sec}s; "
        f"after_log={after_log!r} seen={sorted(seen_candidates)} latest_logs={latest}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Gracefully stop the background server")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    parser.add_argument(
        "--open-remote",
        nargs="?",
        const="latest",
        default=None,
        choices=("latest", "fixed"),
        help="Wait for a new agy Remote Control session and open it in the browser (default: latest; use 'fixed' for the generic entry URL)",
    )
    parser.add_argument("--_wait-remote", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_cid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_pid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_authuser", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._wait_remote is not None:
        try:
            wait_and_open_remote(
                args._wait_remote,
                cid=args._cid,
                project_id=args._pid,
                authuser=args._authuser,
            )
        except Exception:
            _log(f"_wait_remote crashed:\n{traceback.format_exc().rstrip()}")
        return

    if args.stop:
        stop_server(PORT)
        return

    if not is_listening(PORT):
        server_py = Path(__file__).resolve().parent.parent / "server.py"
        if server_py.is_file():
            cmd_args = [str(server_py), "--port", str(PORT)]
            if args.lan:
                cmd_args.append("--lan")
            spawn_detached(cmd_args)

            for _ in range(30):
                if is_listening(PORT):
                    break
                time.sleep(0.05)

    if args.open_remote is not None:
        cid, project_id = get_latest_conversation() if args.open_remote == "latest" else ("", "")
        effective_mode = args.open_remote
        if args.open_remote == "latest" and not cid:
            effective_mode = "fixed"
            _log("main --open-remote latest: no resumable conversation found; falling back to fixed entry URL")
        log_dir = Path.home() / ".gemini" / "antigravity-cli" / "log"
        after_log = max((p.name for p in log_dir.glob("cli-*.log")), default="") if log_dir.is_dir() else ""
        wait_args = [str(Path(__file__).resolve()), "--_wait-remote", after_log]
        if cid:
            wait_args.extend(["--_cid", cid])
            if project_id:
                wait_args.extend(["--_pid", project_id])
        authuser = get_active_user_email()
        if authuser:
            wait_args.extend(["--_authuser", authuser])
        _log(
            f"main --open-remote={args.open_remote} (effective={effective_mode}) authuser={authuser!r} "
            f"cid={cid!r} pid={project_id!r} after_log={after_log!r}; spawning waiter"
        )
        spawn_detached(wait_args)


if __name__ == "__main__":
    main()
