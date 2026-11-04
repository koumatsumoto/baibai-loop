"""Receive planned J-Quants originals without reading or writing L2."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.acquisition import acquire_request
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsAcquisitionError
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.jquants_financial_plan import FrozenFinancialPlan


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


def receive_financial_handoff(
    directory: Path,
    days: Sequence[date],
    *,
    asof: date,
    started_at: datetime,
    client: JQuantsHTTP,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Build a source handoff even when acquisition stopped after partial receipt."""
    if started_at.utcoffset() is None:
        raise ValueError("started_at requires a timezone")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "receipts.sqlite"
    failed = False
    try:
        receive_financial_days(output, days, client=client, sleep=sleep)
    except JQuantsAcquisitionError:
        failed = True
    with closing(sqlite3.connect(output.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT request_id FROM jquants_source_pages ORDER BY request_id"
            )
        ]
    write_json_atomic(
        directory / "result.json",
        {
            "source": "jquants-financial",
            "asof": asof.isoformat(),
            "started_at": started_at.isoformat(),
            "request_ids": ids,
            "acquisition": {"status": "failed" if failed else "received"},
        },
    )
    return not failed


def receive_planned_handoff(
    directory: Path,
    frozen: FrozenFinancialPlan,
    *,
    asof: date,
    client: JQuantsHTTP,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Consume a frozen L1 plan without treating unavailable sources as empty."""
    if frozen.result.plan.unknown_incomplete_requests:
        raise ValueError("cannot acquire from a plan with unavailable originals")
    return receive_financial_handoff(
        directory,
        frozen.result.plan.days,
        asof=asof,
        started_at=frozen.known_at,
        client=client,
        sleep=sleep,
    )
