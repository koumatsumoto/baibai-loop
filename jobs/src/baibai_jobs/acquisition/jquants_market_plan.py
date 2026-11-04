"""Plan native daily prices from a fixed L1 release and pending originals."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource, RetainedRequest
from baibai_engine.data_collection.providers.jquants.dependencies import (
    dependency_covers_day,
    dependency_from_request,
)
from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    fixed_complete_request_ids,
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.market_rules import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    CALENDAR_FORWARD_DAYS,
    CALENDAR_LOOKBACK_DAYS,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
    MARKET_REVISION_OVERLAP_DAYS,
    WEEKLY_BALANCE_LOOKBACK_DAYS,
)
from baibai_engine.data_collection.providers.jquants.native_index import (
    MarginAcquisitionPlan,
    NativeAcquisitionPlan,
    NativeMarketIndex,
    index_market_originals,
    plan_calendar,
    plan_daily_margin,
    plan_daily_prices,
    plan_required_days,
    plan_weekly_margin,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.primitives.market_windows import FINANCIAL_HISTORY_DAYS
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import FixedRelease, accepted_dataset
from baibai_jobs.acquisition.jquants_pending import (
    iter_pending_requests,
    open_pending_originals,
)
from baibai_jobs.acquisition.receipts import ReceiptStore


@dataclass(frozen=True, slots=True)
class FixedPricePlan:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    plan: NativeAcquisitionPlan


def plan_fixed_prices(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    start: date,
    end: date,
    known_at: datetime,
    pending_paths: Sequence[Path] = (),
    unavailable_stages: Sequence[str] = (),
) -> FixedPricePlan:
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)

    def originals() -> Iterator[RetainedRequest]:
        yield from iter_fixed_retained_requests(
            session, release=release, cache=cache, source="daily_bars", known_at=known_at
        )
        for path in pending_paths:
            yield from iter_pending_requests(path, source="daily_bars", known_at=known_at)
        for stage in unavailable_stages:
            yield RetainedRequest(f"stage:{stage}", None, "unavailable handoff")

    plan = plan_daily_prices(
        index_market_originals(originals(), source="daily_bars"), start=start, end=end
    )
    return FixedPricePlan(
        release.release_id,
        release.manifest_sha256,
        release.dataset_manifest(dataset.name).coverage_status,
        plan,
    )


@dataclass(frozen=True, slots=True)
class FixedMarketIndexes:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    known_at: datetime
    indexes: dict[JQuantsSource, NativeMarketIndex]
    unavailable_stages: tuple[str, ...]
    dependencies: tuple[dict[str, Any], ...]


def freeze_market_originals(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
    asof: date,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FixedMarketIndexes:
    """Freeze L1 and pending market originals once; never consult an L2 input."""
    sources: tuple[JQuantsSource, ...] = (
        "master",
        "daily_bars",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    cutoff = clock()
    if cutoff.utcoffset() is None:
        raise ValueError("clock requires a timezone")
    with open_pending_originals(
        store,
        release=release,
        source="jquants-market",
        clock=lambda: cutoff,
        verify_l1=lambda ids: fixed_complete_request_ids(
            session, release=release, cache=cache, known_at=cutoff, candidates=ids
        ),
    ) as (
        known_at,
        pending,
        missing,
    ):
        dependencies: dict[JQuantsSource, tuple[datetime, str, dict[str, Any]]] = {}

        def originals(source: JQuantsSource) -> Iterator[RetainedRequest]:
            def inspect(items: Iterable[RetainedRequest]) -> Iterator[RetainedRequest]:
                for item in items:
                    request = item.request
                    if (
                        request is not None
                        and source in {"master", "market_calendar"}
                        and (dependency_covers_day(source, request.params, asof))
                    ):
                        old = dependencies.get(source)
                        if old is None or (request.received_at, item.request_id) > (old[0], old[1]):
                            dependencies[source] = (
                                request.received_at,
                                item.request_id,
                                dependency_from_request(item.request_id, request),
                            )
                    yield item

            yield from inspect(
                iter_fixed_retained_requests(
                    session, release=release, cache=cache, source=source, known_at=known_at
                )
            )
            for _, path in pending:
                yield from inspect(iter_pending_requests(path, source=source, known_at=known_at))
            for stage in missing:
                yield RetainedRequest(f"stage:{stage}", None, "unavailable handoff")

        indexes = {
            source: index_market_originals(originals(source), source=source) for source in sources
        }
        for source, old in tuple(dependencies.items()):
            for failed in indexes[source].incomplete:
                query = dict(failed.query or ())
                if (not query or dependency_covers_day(source, query, asof)) and (
                    failed.received_at is None or failed.received_at >= old[0]
                ):
                    dependencies.pop(source)
                    break
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    return FixedMarketIndexes(
        release.release_id,
        release.manifest_sha256,
        release.dataset_manifest(dataset.name).coverage_status,
        known_at,
        indexes,
        tuple(sorted(missing)),
        tuple(item[2] for _, item in sorted(dependencies.items())),
    )


@dataclass(frozen=True, slots=True)
class MarketPlans:
    master: NativeAcquisitionPlan
    daily_prices: NativeAcquisitionPlan
    calendar: NativeAcquisitionPlan
    weekly_margin: MarginAcquisitionPlan
    daily_margin: MarginAcquisitionPlan
    margin_alerts: NativeAcquisitionPlan
    short_sales: NativeAcquisitionPlan


def plan_market_from_originals(
    indexes: dict[JQuantsSource, NativeMarketIndex], *, asof: date
) -> MarketPlans:
    """Plan each source from original receipts; unavailable scopes stay explicit."""

    def refresh_tail(source: JQuantsSource) -> NativeAcquisitionPlan:
        index = indexes[source]
        start = asof - timedelta(days=MARKET_REVISION_OVERLAP_DAYS)
        earliest = min((receipt.start for receipt in index.receipts), default=start)
        first = min(start, earliest)
        days = (first + timedelta(days=offset) for offset in range((asof - first).days + 1))
        missing = plan_required_days(index, days=days, require_nonempty=False)
        tail = (start + timedelta(days=offset) for offset in range((asof - start).days + 1))
        return NativeAcquisitionPlan(
            tuple(sorted(set(missing.days) | set(tail))), missing.unknown_incomplete_requests
        )

    calendar = indexes["market_calendar"]
    calendar_start = asof - timedelta(days=CALENDAR_LOOKBACK_DAYS)
    earliest_calendar = min((item.start for item in calendar.receipts), default=calendar_start)
    return MarketPlans(
        master=plan_required_days(indexes["master"], days=[asof], require_nonempty=True),
        daily_prices=plan_daily_prices(
            indexes["daily_bars"], start=asof - timedelta(days=FINANCIAL_HISTORY_DAYS), end=asof
        ),
        calendar=plan_calendar(
            calendar,
            start=min(calendar_start, earliest_calendar),
            end=asof + timedelta(days=CALENDAR_FORWARD_DAYS),
        ),
        weekly_margin=plan_weekly_margin(
            indexes["margin_interest"],
            calendar,
            start=asof - timedelta(days=WEEKLY_BALANCE_LOOKBACK_DAYS),
            asof=asof,
            final_balance_date=LEGACY_WEEKLY_LAST_BALANCE_DATE,
            final_publication_date=LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
        ),
        daily_margin=plan_daily_margin(
            indexes["margin_interest"],
            calendar,
            first_balance_date=ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
            asof=asof,
        ),
        margin_alerts=refresh_tail("margin_alerts"),
        short_sales=refresh_tail("short_sale_reports"),
    )
