#!/usr/bin/env python3
"""Print the latest top-level agy conversation ID with a valid local project (fallback to latest top-level)."""

import re
import sqlite3
import sys
from pathlib import Path

_CID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _has_valid_project(pid: str, projects_dir: Path) -> bool:
    return bool(pid and _PID_RE.fullmatch(pid) and (projects_dir / f"{pid}.json").is_file())


def get_latest_conversation_id(
    db_path: Path | None = None,
    projects_dir: Path | None = None,
) -> str:
    path = db_path or (Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")
    if not path.is_file():
        return ""
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
                return cid
        return fallback_cid
    except Exception:
        return ""
    finally:
        if con is not None:
            con.close()


def main() -> int:
    cid = get_latest_conversation_id()
    if not cid:
        print("No resumable conversation found.", file=sys.stderr)
        return 1
    print(cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
