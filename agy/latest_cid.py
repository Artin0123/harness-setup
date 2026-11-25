#!/usr/bin/env python3
"""Print the latest top-level agy conversation ID and exit immediately."""

import re
import sqlite3
import sys
from pathlib import Path

_CID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def get_latest_conversation_id(db_path: Path | None = None) -> str:
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


def main() -> int:
    cid = get_latest_conversation_id()
    if not cid:
        print("No resumable conversation found.", file=sys.stderr)
        return 1
    print(cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
