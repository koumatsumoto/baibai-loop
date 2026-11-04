"""Compose a fixed L1-only financial rebuild into a caller-owned DuckDB session."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    accepted_dataset,
)


@dataclass(frozen=True, slots=True)
class FinancialRebuildReport:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    requested_days: int
    received_days: int
    unavailable_days: int
    partial_days: int
    rows: int


def rebuild_fixed_financial_days(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    days: Iterable[date],
    known_at: datetime,
) -> FinancialRebuildReport:
    """Read verified L1 objects and build isolated L2 tables, with no previous L2.

    Partitions are selected by actual receipt month, not the disclosure date.
    L1 coverage and per-day request state remain separate in the result.
    """
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    wanted = tuple(sorted(set(days)))
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    requests = (
        iter_fixed_retained_requests(
            session, release=release, cache=cache, source="financial_summaries", known_at=known_at
        )
        if wanted
        else ()
    )
    results = rebuild_financial_days(requests, days=wanted)
    materialize_financial_days(
        session.connection,
        results.values(),
        l1_coverage_status=release.dataset_manifest(dataset.name).coverage_status,
    )
    return FinancialRebuildReport(
        release_id=release.release_id,
        manifest_sha256=release.manifest_sha256,
        l1_coverage_status=release.dataset_manifest(dataset.name).coverage_status,
        requested_days=len(wanted),
        received_days=sum(item.request_id is not None for item in results.values()),
        unavailable_days=sum(item.request_id is None for item in results.values()),
        partial_days=sum(
            item.status == "partial" or item.incomplete_requests > 0 for item in results.values()
        ),
        rows=sum(len(item.rows) for item in results.values()),
    )
