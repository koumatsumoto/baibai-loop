"""Read pending J-Quants original pages without loading or projecting L2."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.retained import decode_retained_rows


def iter_pending_financial_requests(path: Path, *, known_at: datetime) -> Iterator[RetainedRequest]:
    """Read one verified private handoff; caller owns its retention and digest check."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    if not path.is_absolute() or not path.is_file():
        raise ValueError("verified local handoff is required")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT request_id, page_index, source, query_json, received_at_utc, body_gzip "
            "FROM jquants_source_pages WHERE request_id IN "
            "(SELECT request_id FROM jquants_source_pages WHERE source = ?) "
            "ORDER BY request_id, page_index",
            ("financial_summaries",),
        )
        current: str | None = None
        pages: list[tuple[object, ...]] = []
        for row in rows:
            identity = str(row[0])
            if current is not None and identity != current:
                if pages:
                    yield decode_retained_rows(current, pages)
                pages = []
            current = identity
            try:
                received = datetime.fromisoformat(str(row[4]))
                if received.utcoffset() is None:
                    raise ValueError("receipt time requires a timezone")
            except ValueError:
                # Keep the malformed page so it becomes an explicit incomplete receipt.
                pages.append(row)
            else:
                if received <= known_at:
                    pages.append(row)
        if current is not None and pages:
            yield decode_retained_rows(current, pages)
