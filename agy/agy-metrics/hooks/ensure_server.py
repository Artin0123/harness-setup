#!/usr/bin/env python3
"""Ensure agy-metrics backend server is running in background on 127.0.0.1:47891."""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 47891
BASE_URL = "https://antigravity.google.com"
REMOTE_PATH = "/remote?path=%2Fview%2Fremote-control"
REMOTE_URL = f"{BASE_URL}{REMOTE_PATH}"
_CID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_REMOTE_CONNECTED_RE = re.compile(
    r"\[remote-control-([0-9a-fA-F-]+-v2)\] Connection status: Connected"
)


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


def get_latest_conversation(db_path: Path | None = None) -> tuple[str, str]:
    """Return (conversation_id, project_id) of the latest top-level conversation."""
    import sqlite3

    path = db_path or (Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")
    if not path.is_file():
        return "", ""
    con = None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        row = con.execute(
            "SELECT conversation_id, project_id FROM conversation_summaries "
            "WHERE length(parent_conversation_id) = 0 "
            "ORDER BY last_modified_time DESC LIMIT 1"
        ).fetchone()
        if not row or not row[0]:
            return "", ""
        cid = str(row[0])
        if not _CID_RE.fullmatch(cid):
            return "", ""
        pid = str(row[1]) if len(row) > 1 and row[1] else ""
        if pid and not _CID_RE.fullmatch(pid):
            pid = ""
        return cid, pid
    except Exception:
        return "", ""
    finally:
        if con is not None:
            con.close()


def build_remote_url(
    session_id: str,
    cid: str = "",
    project_id: str = "",
    user: int | None = None,
) -> str:
    prefix = f"{BASE_URL}/u/{user}" if user is not None else BASE_URL
    if not cid:
        return f"{prefix}{REMOTE_PATH}"
    inner = f"c/{cid}"
    if project_id:
        inner = f"{inner}?project={project_id}"
    return f"{prefix}/r/{session_id}?p={urllib.parse.quote(inner, safe='')}"


def wait_and_open_remote(
    after_log: str = "",
    cid: str = "",
    project_id: str = "",
    user: int | None = None,
    log_dir: Path | None = None,
    timeout_sec: float = 20.0,
) -> None:
    import webbrowser

    directory = log_dir or (Path.home() / ".gemini" / "antigravity-cli" / "log")
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if directory.is_dir():
            candidates = sorted(
                (p for p in directory.glob("cli-*.log") if p.name > after_log),
                key=lambda p: p.name,
                reverse=True,
            )
            for log_file in candidates:
                try:
                    text = log_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m = _REMOTE_CONNECTED_RE.search(text)
                if m:
                    webbrowser.open(build_remote_url(m.group(1), cid, project_id, user=user))
                    return
        time.sleep(0.25)


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
    parser.add_argument(
        "--user",
        type=int,
        default=None,
        help="Google account index for /u/<index> in the Remote Control URL (e.g. 2)",
    )
    parser.add_argument("--_wait-remote", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_cid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_pid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_user", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.user is not None and args.user < 0:
        parser.error("--user must be >= 0")

    if args._wait_remote is not None:
        wait_and_open_remote(
            args._wait_remote,
            cid=args._cid,
            project_id=args._pid,
            user=args._user,
        )
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
        if args.open_remote == "latest" and not cid:
            print("No resumable conversation found for --open-remote.", file=sys.stderr)
            return
        log_dir = Path.home() / ".gemini" / "antigravity-cli" / "log"
        after_log = max((p.name for p in log_dir.glob("cli-*.log")), default="") if log_dir.is_dir() else ""
        wait_args = [str(Path(__file__).resolve()), "--_wait-remote", after_log]
        if cid:
            wait_args.extend(["--_cid", cid])
            if project_id:
                wait_args.extend(["--_pid", project_id])
        if args.user is not None:
            wait_args.extend(["--_user", str(args.user)])
        spawn_detached(wait_args)


if __name__ == "__main__":
    main()
