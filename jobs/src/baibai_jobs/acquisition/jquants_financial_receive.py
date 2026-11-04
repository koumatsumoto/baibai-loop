"""Receive planned J-Quants originals without reading or writing L2."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.acquisition import acquire_request
from baibai_engine.data_collection.providers.jquants.calendar import business_day_from_originals
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsAcquisitionError
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import FixedRelease
from baibai_jobs.acquisition.jquants_financial_plan import (
    FrozenFinancialPlan,
    freeze_financial_acquisition_plan,
)
from baibai_jobs.acquisition.jquants_pending import (
    retained_snapshot_time,
    select_pending_financial_handoffs,
)
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs


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
    with closing(sqlite3.connect(output)) as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
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
    except (JQuantsAcquisitionError, AcquisitionDeadlineExceeded):
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


def run_l1_financial_acquisition(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
    directory: Path,
    asof: date,
    scheduled: bool,
    lookback_days: int,
    revision_overlap_days: int,
    client: JQuantsHTTP,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    """Build one financial handoff using fixed originals rather than L2 state."""
    if lookback_days < 0:
        raise ValueError("lookback days must not be negative")
    start = asof - timedelta(days=lookback_days)
    frozen = freeze_financial_acquisition_plan(
        session,
        release=release,
        cache=cache,
        store=store,
        start=start,
        end=asof,
        revision_overlap_days=revision_overlap_days,
        clock=clock,
    )
    if frozen.result.plan.unknown_incomplete_requests:
        raise ValueError("cannot acquire from a plan with unavailable originals")
    if scheduled:
        entries = available_handoffs(store, source="jquants-market", include_integrated=True)
        pending, unavailable = select_pending_financial_handoffs(
            entries, captured_at=retained_snapshot_time(release), source="jquants-market"
        )
        if not pending and not unavailable:
            originals = iter_fixed_retained_requests(
                session,
                release=release,
                cache=cache,
                source="market_calendar",
                known_at=frozen.known_at,
            )
            if business_day_from_originals(originals, asof) is False:
                ok = receive_financial_handoff(
                    directory, (), asof=asof, started_at=frozen.known_at, client=client
                )
                report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                report["acquisition"] = {"status": "skipped_non_business_day"}
                write_json_atomic(directory / "result.json", report)
                return ok
    return receive_planned_handoff(directory, frozen, asof=asof, client=client)
