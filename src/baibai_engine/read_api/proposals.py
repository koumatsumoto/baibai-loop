"""Query-only proposal views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows


def list_proposal_payloads(path: Path) -> list[dict[str, object]]:
    rows = read_rows(
        path,
        "SELECT proposal_id, ticker, thesis_id, review_id, created_at, "
        "status, decided_at, payload FROM proposal "
        "ORDER BY created_at DESC, proposal_id DESC",
    )
    return [
        {
            "proposal_id": row["proposal_id"],
            "ticker": row["ticker"],
            "thesis_id": row["thesis_id"],
            "review_id": row["review_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "decided_at": row["decided_at"],
            "payload": json.loads(str(row["payload"])),
        }
        for row in rows
    ]


__all__ = ["list_proposal_payloads"]
