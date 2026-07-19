"""Query-only proposal views."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .sqlite import connect_read_only


def list_proposal_payloads(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    try:
        with closing(connect_read_only(path)) as connection:
            rows = connection.execute(
                "SELECT proposal_id, ticker, packet_id, review_id, created_at, "
                "status, decided_at, payload FROM proposal "
                "ORDER BY created_at DESC, proposal_id DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "proposal_id": row["proposal_id"],
            "ticker": row["ticker"],
            "packet_id": row["packet_id"],
            "review_id": row["review_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "decided_at": row["decided_at"],
            "payload": json.loads(str(row["payload"])),
        }
        for row in rows
    ]


__all__ = ["list_proposal_payloads"]
