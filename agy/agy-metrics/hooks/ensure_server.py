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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Gracefully stop the background server")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    args = parser.parse_args()

    if args.stop:
        stop_server(PORT)
        return

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
