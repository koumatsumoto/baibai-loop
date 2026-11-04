"""Retained J-Quants response pages, independent of their L2 interpretation.

The caller provisions the shared market store and supplies a dedicated connection.
A received page is committed before JSON parsing or the next HTTP request. A
complete request is established by its stored pagination chain, not by an L2 row
count. Failed requests can therefore leave useful, explicitly incomplete evidence.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRequest,
    JQuantsSource,
    complete_request,
)


def import_requests(
    source: sqlite3.Connection, target: sqlite3.Connection, request_ids: Sequence[str]
) -> int:
    """Copy only original pages; incomplete requests remain explicitly incomplete.

    The caller owns the transaction and the market schema. Shared receipt IDs
    must carry identical bytes and metadata; this never imports projected facts.
    """
    added = 0
    for request_id in request_ids:
        rows = source.execute(
            "SELECT * FROM jquants_source_pages WHERE request_id = ? ORDER BY page_index",
            (request_id,),
        ).fetchall()
        if not rows:
            raise ValueError("J-Quants handoff request is not retained")
        for row in rows:
            existing = target.execute(
                "SELECT * FROM jquants_source_pages WHERE request_id = ? AND page_index = ?",
                (row[0], row[1]),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != tuple(row):
                    raise ValueError("J-Quants handoff receipt identity conflicts")
                continue
            target.execute("INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)", row)
            added += 1
    return added


def read_request_pages(conn: sqlite3.Connection, request_id: str) -> tuple[JQuantsPage, ...]:
    """Read retained bodies, including bodies that cannot be decoded as valid JSON."""
    rows = conn.execute(
        "SELECT page_index, source, query_json, received_at_utc, body_gzip "
        "FROM jquants_source_pages WHERE request_id = ? ORDER BY page_index",
        (request_id,),
    ).fetchall()
    if not rows:
        raise ValueError("J-Quants request is not retained")
    pages: list[JQuantsPage] = []
    for expected, (index, source, query_json, received_at, body) in enumerate(rows):
        if index != expected:
            raise ValueError("J-Quants request has a missing page")
        query = json.loads(query_json)
        if not isinstance(query, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in query.items()
        ):
            raise ValueError("invalid retained J-Quants query")
        pages.append(
            JQuantsPage(
                cast("JQuantsSource", source),
                query,
                datetime.fromisoformat(received_at),
                gzip.decompress(body),
            )
        )
    return tuple(pages)


def read_complete_request(conn: sqlite3.Connection, request_id: str) -> JQuantsRequest:
    """Require a closed, ordered source pagination chain before L2 uses its rows."""
    return complete_request(read_request_pages(conn, request_id))
