"""Derive a financial L2 snapshot directly from a fixed native L1 dataset."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.lake_receipts import iter_retained_requests
from baibai_engine.data_collection.providers.jquants.native_index import (
    index_market_originals,
    plan_required_days,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    JQUANTS_FIN_SUMMARIES,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.models import CoverageStatus, validate_dataset_contract
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.writer import LakeBuildReport, export_l1_derived_dataset


@dataclass(frozen=True, slots=True)
class FinancialExport:
    build: LakeBuildReport
    source_days: int
    missing_days: int
    partial_days: int
    unknown_requests: int


def export_financial_from_l1(
    *,
    source: LakeBuildReport,
    mirror_root: Path,
    producer_git_commit: str,
    known_at: datetime,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> FinancialExport:
    """Build every retained full-day financial observation without an earlier L2."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    validate_dataset_contract(source.manifest, JQUANTS_SOURCE_PAGES)
    payload = source.manifest_path.read_bytes()
    source_sha = hashlib.sha256(payload).hexdigest()
    cache = LakeObjectCache(root=mirror_root, source=LocalMirrorSource(mirror_root))
    paths = materialize_partitions(
        cache, dataset=JQUANTS_SOURCE_PAGES, partitions=source.manifest.partitions
    )
    with lake_session() as session:
        future_rows = session.connection.execute(
            "SELECT COUNT(*) FROM read_parquet($files, union_by_name = false) "
            "WHERE source = 'financial_summaries' "
            "AND CAST(received_at_utc AS TIMESTAMPTZ) > $cutoff",
            {"files": [str(path) for path in paths], "cutoff": known_at},
        ).fetchone()
        if future_rows is None:
            raise ValueError("financial source timestamp check failed")

        def requests() -> Iterator[RetainedRequest]:
            return iter_retained_requests(
                session.connection, paths, source="financial_summaries", known_at=known_at
            )

        index = index_market_originals(requests(), source="financial_summaries")
        days = {item.start for item in index.receipts}
        # Include later incomplete requests in the horizon: omitting them would
        # silently publish the previous day's data as a complete latest result.
        for item in index.incomplete:
            query = dict(item.query or ())
            if set(query) == {"date"}:
                # The planner reports unparseable requests as unknown.
                with suppress(ValueError):
                    days.add(date.fromisoformat(query["date"]))
        if not days:
            raise ValueError("no complete native financial requests are available")
        start, end = min(days), max(days)
        horizon = (start + timedelta(days=offset) for offset in range((end - start).days + 1))
        gaps = plan_required_days(index, days=horizon, require_nonempty=False)
        results = rebuild_financial_days(requests(), days=days)
        materialize_financial_days(
            session.connection, results.values(), l1_coverage_status=source.manifest.coverage_status
        )
        partial_days = sum(
            item.status != "ok" or item.incomplete_requests > 0 for item in results.values()
        )
        coverage: CoverageStatus = (
            "partial"
            if gaps.days or gaps.unknown_incomplete_requests or partial_days or future_rows[0]
            else "complete"
        )
        build = export_l1_derived_dataset(
            dataset=JQUANTS_FIN_SUMMARIES,
            source_manifest=source.manifest,
            source_manifest_sha256=source_sha,
            connection=session.connection,
            table="financial_summaries",
            coverage_status=coverage,
            mirror_root=mirror_root,
            producer_git_commit=producer_git_commit,
            build_id=build_id,
            created_at=created_at,
        )
    return FinancialExport(
        build,
        len(days),
        len(gaps.days),
        partial_days,
        len(gaps.unknown_incomplete_requests) + int(future_rows[0]),
    )
