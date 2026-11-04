"""Plan J-Quants financial acquisition from one fixed L1 release."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.financial_plan import (
    plan_financial_acquisition,
)
from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.native_index import NativeAcquisitionPlan
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
class FixedFinancialPlan:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    plan: NativeAcquisitionPlan


def plan_fixed_financial_acquisition(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    start: date,
    end: date,
    known_at: datetime,
    revision_overlap_days: int,
    pending_paths: Sequence[Path] = (),
    unavailable_stages: Sequence[str] = (),
) -> FixedFinancialPlan:
    """Plan from retained native requests; no previous L2 or scoped input DB."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    if start > end or revision_overlap_days < 0:
        raise ValueError("invalid acquisition interval or revision overlap")
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)

    def originals() -> Iterator[RetainedRequest]:
        yield from iter_fixed_retained_requests(
            session, release=release, cache=cache, source="financial_summaries", known_at=known_at
        )
        for path in pending_paths:
            yield from iter_pending_requests(path, source="financial_summaries", known_at=known_at)
        for stage in unavailable_stages:
            yield RetainedRequest(f"stage:{stage}", None, "unavailable handoff")

    requests = originals()
    plan = plan_financial_acquisition(
        requests, start=start, end=end, revision_overlap_days=revision_overlap_days
    )
    return FixedFinancialPlan(
        release_id=release.release_id,
        manifest_sha256=release.manifest_sha256,
        l1_coverage_status=release.dataset_manifest(dataset.name).coverage_status,
        plan=plan,
    )


@dataclass(frozen=True, slots=True)
class FrozenFinancialPlan:
    result: FixedFinancialPlan
    known_at: datetime
    pending_stages: tuple[str, ...]
    unavailable_stages: tuple[str, ...]


def freeze_financial_acquisition_plan(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
    start: date,
    end: date,
    revision_overlap_days: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FrozenFinancialPlan:
    """Freeze available original handoffs and plan without consulting L2.

    A missing or changed handoff is recorded, not mistaken for an empty source.
    Files are transient and fixed by their handoff digest before reading.
    """
    with open_pending_originals(
        store, release=release, source="jquants-financial", clock=clock
    ) as (known_at, pending, missing):
        result = plan_fixed_financial_acquisition(
            session,
            release=release,
            cache=cache,
            start=start,
            end=end,
            known_at=known_at,
            revision_overlap_days=revision_overlap_days,
            pending_paths=[path for _, path in pending],
            unavailable_stages=missing,
        )
    return FrozenFinancialPlan(
        result=result,
        known_at=known_at,
        pending_stages=tuple(stage for stage, _ in pending),
        unavailable_stages=missing,
    )
