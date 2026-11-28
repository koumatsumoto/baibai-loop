"""Run the US L1 inputs alongside, not inside, the JP Machine publication."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from httpx2 import BaseTransport, HTTPTransport

from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarReading,
    USRegularSession,
)
from baibai_engine.data_collection.providers.alpaca_http import (
    AlpacaCredentials,
    receive_paper_us_assets,
)
from baibai_engine.data_collection.providers.alpaca_sip_native import (
    SavedAlpacaSIPDay,
    read_alpaca_sip_day,
)
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    ALPACA_ASSETS_SOURCE,
    CurrentListingMarket,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store, R2OperationStopped
from baibai_jobs.integration.listing_master_current import (
    ListingMasterStore,
    apply_listing_market_update,
    read_current_listing_master,
)
from baibai_jobs.integration.us_daily_handoff import (
    alpaca_paper_credentials_from_env,
    receive_us_daily_calendar,
    receive_us_daily_prices,
)
from baibai_jobs.integration.us_daily_native import write_alpaca_sip_day
from baibai_jobs.integration.us_daily_prices import latest_closed_us_session
from baibai_jobs.schedule import us_market_run_origin

_NY = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(UTC)


def _usable_master(current: CurrentListingMarket | None, session: USRegularSession) -> bool:
    return (
        current is not None
        and current.source == ALPACA_ASSETS_SOURCE
        and current.slice.session == session.trading_date
        and current.received_at < session.close_at
    )


def _receipts(status: str, saved: SavedAlpacaSIPDay) -> dict[str, object]:
    day = saved.day
    return {
        "status": status,
        "session": day.session.isoformat(),
        "expected": day.expected,
        "observed": len(day.bars),
        "missing": len(day.missing),
        "unverified": len(day.unverified),
    }


def _save_previous_prices(
    store: ListingMasterStore,
    current: CurrentListingMarket | None,
    calendar: AlpacaCalendarReading,
    *,
    origin: datetime,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    session = latest_closed_us_session(calendar, cutoff=clock())
    if session is None:
        return {"status": "no_closed_session"}
    day = session.trading_date
    # Scheduling delay must not retarget a previous-session run to the next close.
    if session.close_at > origin:
        return {"status": "run_late", "session": day.isoformat()}
    saved = read_alpaca_sip_day(store, session=day)
    if saved is not None:
        if saved.day.closed_at != session.close_at:
            raise ValueError("stored SIP session differs from the verified close")
        if not saved.day.unverified:
            return _receipts("already_saved", saved)
    if not _usable_master(current, session):
        return {"status": "preclose_master_unavailable", "session": day.isoformat()}
    received = receive_us_daily_prices(
        current,
        calendar=calendar,
        credentials=credentials,
        transport=transport,
        clock=clock,
        attempt_id=uuid4(),
        expected_session=day,
    )
    if received is None:
        return {"status": "prices_unavailable", "session": day.isoformat()}
    http_errors = sorted(
        {
            capture.pages[-1].response.status_code
            for receipt in received.receipts
            if (capture := receipt.capture) is not None and capture.failure == "http_status"
        }
    )
    stored = write_alpaca_sip_day(store, received)
    result: dict[str, object] = (
        {"status": "prices_unavailable", "session": day.isoformat()}
        if stored is None
        else _receipts("partial" if stored.day.unverified else "saved", stored)
    )
    if http_errors:
        result["http_errors"] = http_errors
    return result


def _refresh_current_master(
    store: ListingMasterStore,
    current: CurrentListingMarket | None,
    session: USRegularSession | None,
    *,
    now_ny_day: date,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    if session is None:
        return {"status": "market_closed", "session": now_ny_day.isoformat()}
    day = session.trading_date.isoformat()
    if _usable_master(current, session):
        return {"status": "already_current", "session": day}
    if clock() >= session.close_at:
        return {"status": "refresh_window_closed", "session": day}
    receipt = receive_paper_us_assets(credentials=credentials, transport=transport, clock=clock)
    if receipt.received_at >= session.close_at:
        return {"status": "refresh_window_closed", "session": day}
    if receipt.received_at.astimezone(_NY).date() != session.trading_date:
        raise ValueError("Alpaca Assets receipt does not match the current NY session")

    def replace_us(prior: CurrentListingMarket | None) -> CurrentListingMarket | None:
        # Replan only against the same current-file CAS, preserving a concurrent JP update.
        if _usable_master(prior, session):
            return prior
        if prior is not None and prior.slice.session > session.trading_date:
            return None
        return prepare_alpaca_research_inventory(receipt, previous=prior).current

    status = apply_listing_market_update(store, "US", replace_us)
    return {"status": status, "session": day, "received_assets": len(receipt.assets)}


def run_us_market_daily(
    store: ListingMasterStore,
    *,
    run_created_at: datetime,
    scheduled: bool,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> tuple[int, dict[str, object]]:
    origin = us_market_run_origin(run_created_at, clock(), scheduled=scheduled)
    today = origin.astimezone(_NY).date()
    calendar, wire = receive_us_daily_calendar(
        credentials=credentials, transport=transport, clock=clock
    )
    checked_at = clock()
    if (
        calendar.problem is not None
        or calendar.received_at is None
        or checked_at.utcoffset() is None
        or calendar.received_at > checked_at
        or checked_at.astimezone(_NY).date() != today
        or calendar.is_open(today) is None
    ):
        denied = {"http_status": wire.response.status_code} if wire is not None else {}
        return 1, {"status": "calendar_unavailable", "reason": calendar.problem, **denied}
    previous = read_current_listing_master(store).get("US")
    try:
        prices = _save_previous_prices(
            store,
            previous,
            calendar,
            origin=origin,
            credentials=credentials,
            transport=transport,
            clock=clock,
        )
    except (Exception, R2OperationStopped) as error:
        prices = {"status": "failed", "reason": type(error).__name__}
    # The current session still needs a pre-close master for the next run, even
    # when the earlier price collection failed. Do not publish prices from this refresh.
    try:
        master = _refresh_current_master(
            store,
            previous,
            calendar.session(today),
            now_ny_day=today,
            credentials=credentials,
            transport=transport,
            clock=clock,
        )
    except (Exception, R2OperationStopped) as error:
        master = {"status": "failed", "reason": type(error).__name__}
    complete = prices["status"] in ("saved", "already_saved", "no_closed_session") and master[
        "status"
    ] in ("market_closed", "already_current", "updated", "unchanged")
    return int(not complete), {"sip_prices": prices, "listing_master": master}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-created-at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--event", required=True, choices=("schedule", "workflow_dispatch"))
    args = parser.parse_args(argv)
    try:
        credentials = alpaca_paper_credentials_from_env()
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        with (
            closing(Boto3R2Store(bucket=bucket, verify_put_with_head=False)) as store,
            HTTPTransport(trust_env=False, retries=0) as transport,
        ):
            code, report = run_us_market_daily(
                store,
                run_created_at=args.run_created_at,
                scheduled=args.event == "schedule",
                credentials=credentials,
                transport=transport,
                clock=_now,
            )
    except (Exception, R2OperationStopped) as error:
        code, report = 1, {"status": "failed", "reason": type(error).__name__}
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
