"""Plan native daily prices from a fixed L1 release and pending originals."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource, RetainedRequest
from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.market_index import (
    NativeAcquisitionPlan,
    NativeMarketIndex,
    index_market_originals,
    plan_daily_prices,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
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


def freeze_market_originals(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
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
    with open_pending_originals(store, release=release, source="jquants-market", clock=clock) as (
        known_at,
        pending,
        missing,
    ):

        def originals(source: JQuantsSource) -> Iterator[RetainedRequest]:
            yield from iter_fixed_retained_requests(
                session, release=release, cache=cache, source=source, known_at=known_at
            )
            for _, path in pending:
                yield from iter_pending_requests(path, source=source, known_at=known_at)
            for stage in missing:
                yield RetainedRequest(f"stage:{stage}", None, "unavailable handoff")

        indexes = {
            source: index_market_originals(originals(source), source=source) for source in sources
        }
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    return FixedMarketIndexes(
        release.release_id,
        release.manifest_sha256,
        release.dataset_manifest(dataset.name).coverage_status,
        known_at,
        indexes,
        tuple(sorted(missing)),
    )
