"""Acquire one US daily-price session from pinned listing and calendar inputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import (
    BarsCapture,
    BarsResponse,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.alpaca_calendar import AlpacaCalendarReading
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    AlpacaDailyObservation,
)
from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyCoverage,
    inspect_alpaca_sip_daily_batch,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
)
from baibai_jobs.integration.us_daily_plan import (
    USDailyPricePlan,
    USPriceTarget,
    plan_current_us_daily_prices,
)

_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class USPriceBatchReceipt:
    capture: BarsCapture | None
    coverage: AlpacaSIPDailyCoverage


@dataclass(frozen=True, slots=True)
class USDailyPriceCollection:
    plan: USDailyPricePlan
    receipts: tuple[USPriceBatchReceipt, ...]

    @property
    def fully_observed(self) -> bool:
        return all(r.coverage.failure is None and not r.coverage.missing for r in self.receipts)

    @property
    def observations(self) -> tuple[tuple[USPriceTarget, AlpacaDailyObservation], ...]:
        """Use the pinned listing identity, not the provider's symbol as an instrument ID."""
        targets = {target.listing_key[1]: target for target in self.plan.targets}
        return tuple(
            (targets[row.returned_symbol], row)
            for receipt in self.receipts
            for row in receipt.coverage.observations
        )


def collect_current_us_daily_prices(
    current: CurrentListingMarket | None,
    calendar: AlpacaCalendarReading,
    *,
    attempt_id: UUID,
    clock: Callable[[], datetime],
    transport: Callable[[str], BarsResponse],
    expected_price_session: date | None = None,
) -> USDailyPriceCollection | None:
    """Collect SIP raw USD observations after the latest confirmed NY regular close.

    Supply a complete authorized calendar covering the current NY date and an
    already closed session, plus one pinned current exchange/broker view. The
    caller supplies the direct whole-response transport and owns any L1 handoff.
    None means the scope was not ready; no request or fallback is attempted.
    """
    cutoff = clock()
    if cutoff.utcoffset() is None:
        raise ValueError("US price acquisition requires a timezone-aware clock")
    today_open = calendar.is_open(cutoff.astimezone(_NY).date())
    if (
        calendar.problem is not None
        or calendar.received_at is None
        or calendar.received_at > cutoff
        or today_open is None
    ):
        return None
    session = max(
        (day for day in calendar.sessions if day.close_at <= cutoff),
        key=lambda day: day.trading_date,
        default=None,
    )
    if session is None or (
        expected_price_session is not None and session.trading_date != expected_price_session
    ):
        return None
    plan = plan_current_us_daily_prices(
        current,
        today_open=today_open,
        price_session=session.trading_date,
        cutoff=cutoff,
        adjustment="raw",
    )
    if plan is None:
        return None

    receipts: list[USPriceBatchReceipt] = []
    stop = False
    for offset, batch in enumerate(plan.batches, 1):
        capture = None
        if not stop:
            capture = capture_daily_bars(
                batch.query, request_id=f"us-daily-{attempt_id}-{offset}", transport=transport
            )
        coverage = inspect_alpaca_sip_daily_batch(batch, capture)
        if capture is not None and capture.pages:
            finished_at = clock()
            if finished_at.utcoffset() is None:
                raise ValueError("US price acquisition requires a timezone-aware clock")
            if any(
                not cutoff <= page.response.received_at <= finished_at for page in capture.pages
            ):
                coverage = AlpacaSIPDailyCoverage(batch, (), "invalid_capture")
            stop = capture.failure == "http_status" and capture.pages[-1].response.status_code in {
                401,
                403,
                429,
            }
        receipts.append(USPriceBatchReceipt(capture, coverage))
    return USDailyPriceCollection(plan, tuple(receipts))
