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
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from baibai_engine.collection.jquants.http import (
    JQuantsAcquisitionError,
    JQuantsHTTP,
    JQuantsPage,
    JQuantsSource,
    page_records,
)


@dataclass(frozen=True, slots=True)
class JQuantsRequest:
    source: JQuantsSource
    params: Mapping[str, str]
    pages: tuple[JQuantsPage, ...]

    @property
    def received_at(self) -> datetime:
        return self.pages[-1].received_at

    def records(self) -> list[dict[str, Any]]:
        return [record for page in self.pages for record in page_records(page)]


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


def acquire_request(
    conn: sqlite3.Connection,
    client: JQuantsHTTP,
    source: JQuantsSource,
    params: Mapping[str, str],
) -> str:
    """Receive one explicit query; no normalized table is read or written here."""
    if conn.in_transaction:
        raise ValueError("acquisition requires a connection without a pending transaction")
    request_id = uuid.uuid4().hex
    try:
        for index, page in enumerate(client.pages(source, params)):
            with conn:
                conn.execute(
                    "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        index,
                        page.source,
                        json.dumps(dict(page.query), sort_keys=True, separators=(",", ":")),
                        page.received_at.isoformat(timespec="microseconds"),
                        page.received_at.date().isoformat(),
                        gzip.compress(page.body, mtime=0),
                    ),
                )
    except JQuantsAcquisitionError as error:
        error.request_id = request_id
        raise
    return request_id


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


def complete_request(pages: Sequence[JQuantsPage]) -> JQuantsRequest:
    """Validate the same native pagination chain in a bounded dependency handoff."""
    if not pages:
        raise JQuantsAcquisitionError("J-Quants request has no pages")
    source = pages[0].source
    base = dict(pages[0].query)
    if "pagination_key" in base:
        raise JQuantsAcquisitionError("J-Quants request starts at a continuation page")
    expected_query = dict(base)
    seen: set[str] = set()
    for index, page in enumerate(pages):
        if page.source != source or dict(page.query) != expected_query:
            raise JQuantsAcquisitionError("J-Quants request pagination identity differs")
        page_records(page)
        payload = json.loads(page.body)
        cursor = payload.get("pagination_key")
        if cursor is None or cursor == "":
            if index != len(pages) - 1:
                raise JQuantsAcquisitionError("J-Quants request continues after its final page")
            return JQuantsRequest(source, pages[0].query, tuple(pages))
        if not isinstance(cursor, str) or cursor in seen:
            raise JQuantsAcquisitionError("invalid or repeated J-Quants pagination key")
        seen.add(cursor)
        expected_query = {**base, "pagination_key": cursor}
    raise JQuantsAcquisitionError("J-Quants request is incomplete")
