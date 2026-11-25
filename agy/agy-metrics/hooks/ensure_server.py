#!/usr/bin/env python3
"""Ensure agy-metrics backend server is running in background on 127.0.0.1:47891."""

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 47891


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


def _import_server():
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    import server
    return server


def ensure_remote_control_daemon() -> None:
    srv = _import_server()
    exe = srv.find_agy_executable()
    if not exe:
        return
    if srv.is_remote_control_daemon_running(exe):
        return
    if srv.start_remote_control_serve(exe):
        time.sleep(1.5)


def stop_remote_control_daemon() -> None:
    srv = _import_server()
    if os.name == "nt":
        pids = srv.find_remote_control_serve_pids()
        srv.stop_remote_control_serve_pids(pids)
    else:
        exe = srv.find_agy_executable()
        if exe:
            subprocess.run([exe, "remote-control", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Stop both agy remote-control serve and agy-metrics server")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    parser.add_argument("--daemon", action="store_true", help="Also ensure agy remote-control serve runs with a hidden console")
    args = parser.parse_args()

    if args.stop:
        stop_remote_control_daemon()
        stop_server(PORT)
        return

    if args.daemon:
        ensure_remote_control_daemon()

    if not is_listening(PORT):
        server_py = Path(__file__).resolve().parent.parent / "server.py"
        if server_py.is_file():
            cmd_args = [str(server_py), "--port", str(PORT)]
            if args.lan:
                cmd_args.append("--lan")
            exe = sys.executable
            if os.name == "nt":
                pythonw = Path(exe).with_name("pythonw.exe")
                if pythonw.is_file():
                    exe = str(pythonw)
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                subprocess.Popen(
                    [exe, *cmd_args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [exe, *cmd_args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )

            for _ in range(30):
                if is_listening(PORT):
                    break
                time.sleep(0.05)


if __name__ == "__main__":
    main()
