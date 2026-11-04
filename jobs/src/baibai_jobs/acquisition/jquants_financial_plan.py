"""Plan J-Quants financial acquisition from one fixed L1 release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.financial_plan import (
    FinancialAcquisitionPlan,
    plan_financial_acquisition,
)
from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import FixedRelease, accepted_dataset


@dataclass(frozen=True, slots=True)
class FixedFinancialPlan:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    plan: FinancialAcquisitionPlan


def plan_fixed_financial_acquisition(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    start: date,
    end: date,
    known_at: datetime,
    revision_overlap_days: int,
) -> FixedFinancialPlan:
    """Plan from retained native requests; no previous L2 or scoped input DB."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    if start > end or revision_overlap_days < 0:
        raise ValueError("invalid acquisition interval or revision overlap")
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    requests = iter_fixed_retained_requests(
        session, release=release, cache=cache, source="financial_summaries", known_at=known_at
    )
    plan = plan_financial_acquisition(
        requests, start=start, end=end, revision_overlap_days=revision_overlap_days
    )
    return FixedFinancialPlan(
        release_id=release.release_id,
        manifest_sha256=release.manifest_sha256,
        l1_coverage_status=release.dataset_manifest(dataset.name).coverage_status,
        plan=plan,
    )
