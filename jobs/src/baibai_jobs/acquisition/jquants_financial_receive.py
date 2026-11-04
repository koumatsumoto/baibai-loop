"""Receive planned J-Quants originals without reading or writing L2."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.acquisition import acquire_request
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def receive_financial_days(
    output: Path,
    days: Sequence[date],
    *,
    client: JQuantsHTTP,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, ...]:
    """Commit each received page to a new small transport, including partial requests.

    The caller exports/stages the transport even on provider failure. A transport
    is deliberately not a prior projection: it contains only newly received pages.
    """
    if output.exists():
        raise FileExistsError(output)
    if list(days) != sorted(set(days)):
        raise ValueError("financial days must be sorted and unique")
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_connection(output)) as conn:
        for index, day in enumerate(days):
            if index and index % 31 == 0:
                sleep(3.0)
            acquire_request(conn, client, "financial_summaries", {"date": day.strftime("%Y%m%d")})
        ids = conn.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages ORDER BY request_id"
        ).fetchall()
    return tuple(str(row[0]) for row in ids)
