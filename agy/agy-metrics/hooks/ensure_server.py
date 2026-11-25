#!/usr/bin/env python3
"""Ensure agy-metrics backend server is running in background on 127.0.0.1:47891."""

import argparse
import functools
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 47891
REMOTE_URL = "https://antigravity.google.com/remote?path=%2Fview%2Fremote-control"
_CID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


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


def get_latest_conversation_id(db_path: Path | None = None) -> str:
    import sqlite3

    path = db_path or (Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")
    if not path.is_file():
        return ""
    con = None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        row = con.execute(
            "SELECT conversation_id FROM conversation_summaries "
            "WHERE length(parent_conversation_id) = 0 "
            "ORDER BY last_modified_time DESC LIMIT 1"
        ).fetchone()
        cid = str(row[0]) if row and row[0] else ""
        return cid if _CID_RE.fullmatch(cid) else ""
    except Exception:
        return ""
    finally:
        if con is not None:
            con.close()


@functools.lru_cache(maxsize=1)
def _win32_api():
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("ucLocalAddr", ctypes.c_ubyte * 16),
            ("dwLocalScopeId", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("ucRemoteAddr", ctypes.c_ubyte * 16),
            ("dwRemoteScopeId", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwState", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    return (
        ctypes,
        wintypes,
        ctypes.windll.kernel32,
        ctypes.windll.iphlpapi,
        PROCESSENTRY32W,
        MIB_TCPROW_OWNER_PID,
        MIB_TCP6ROW_OWNER_PID,
    )


def list_agy_pids() -> set[int]:
    if os.name == "nt":
        ctypes, wintypes, kernel32, _, PROCESSENTRY32W, _, _ = _win32_api()
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return set()
        pids: set[int] = set()
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    if pe.szExeFile.lower() == "agy.exe":
                        pids.add(int(pe.th32ProcessID))
                    if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return pids

    try:
        out = subprocess.check_output(["pgrep", "-x", "agy"], text=True, stderr=subprocess.DEVNULL)
        return {int(x) for x in out.split() if x.isdigit()}
    except Exception:
        return set()


def has_established_https(pids: set[int]) -> bool:
    if not pids:
        return False
    if os.name == "nt":
        ctypes, wintypes, _, iphlpapi, _, MIB_TCPROW_OWNER_PID, MIB_TCP6ROW_OWNER_PID = _win32_api()
        TCP_TABLE_OWNER_PID_CONNECTIONS = 4
        MIB_TCP_STATE_ESTAB = 5

        for af, row_cls in ((2, MIB_TCPROW_OWNER_PID), (23, MIB_TCP6ROW_OWNER_PID)):
            size = wintypes.DWORD(0)
            iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
            if not size.value:
                continue
            buf_size = wintypes.DWORD(size.value + 4096)
            buf = (ctypes.c_ubyte * buf_size.value)()
            if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(buf_size), False, af, TCP_TABLE_OWNER_PID_CONNECTIONS, 0) != 0:
                continue
            count = wintypes.DWORD.from_buffer(buf, 0).value
            row_size = ctypes.sizeof(row_cls)
            if 4 + count * row_size > len(buf):
                continue
            for i in range(count):
                row = row_cls.from_buffer(buf, 4 + i * row_size)
                if (
                    row.dwOwningPid in pids
                    and row.dwState == MIB_TCP_STATE_ESTAB
                    and socket.ntohs(row.dwRemotePort & 0xFFFF) == 443
                ):
                    return True
        return False

    pid_arg = ",".join(str(p) for p in pids)
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-a", "-p", pid_arg, "-iTCP:443", "-sTCP:ESTABLISHED"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return len(out.strip().splitlines()) > 1
    except FileNotFoundError:
        return True
    except Exception:
        return False


def wait_and_open_remote(old_pids: set[int], timeout_sec: float = 20.0) -> None:
    import webbrowser

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        new_pids = list_agy_pids() - old_pids
        if new_pids and has_established_https(new_pids):
            time.sleep(0.5)
            webbrowser.open(REMOTE_URL)
            return
        time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Gracefully stop the background server")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    parser.add_argument(
        "--open-remote",
        action="store_true",
        help="Wait for a new agy process to connect in background and open Remote Control URL",
    )
    parser.add_argument(
        "--latest-id",
        action="store_true",
        help="Print the latest top-level conversation ID to stdout",
    )
    parser.add_argument("--_wait-remote", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args._wait_remote is not None:
        old_pids = {int(x) for x in args._wait_remote.split(",") if x.isdigit()}
        wait_and_open_remote(old_pids)
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

    if args.open_remote:
        old_pids_arg = ",".join(str(p) for p in sorted(list_agy_pids()))
        spawn_detached([str(Path(__file__).resolve()), "--_wait-remote", old_pids_arg])

    if args.latest_id:
        cid = get_latest_conversation_id()
        if cid:
            print(cid)


if __name__ == "__main__":
    main()
