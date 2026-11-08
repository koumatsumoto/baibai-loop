"""Derive a price L2 snapshot from retained native L1 pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import JQUANTS_DAILY_BARS
from baibai_engine.storage.snapshots.models import CoverageStatus
from baibai_engine.storage.snapshots.writer import (
    LakeBuildReport,
    SealedSQLiteSnapshot,
    export_dataset_snapshot,
)


@dataclass(frozen=True, slots=True)
class PriceExport:
    build: LakeBuildReport
    source_days: int
    missing_days: int
    partial_days: int
    unknown_requests: int


def export_prices_from_native_input(
    *,
    source_snapshot: SealedSQLiteSnapshot,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> PriceExport:
    """Export this run's rebuilt seal; never GET/decode native originals a second time."""
    from baibai_engine.machine_analysis.generation.price_inputs import INPUT_PREFIX
    from baibai_engine.storage.snapshots.models import NativeL1SourceRef
    from baibai_jobs.integration.price_native import saved_days

    days = saved_days(saved)
    assert saved.descriptor is not None
    digest = saved.descriptor["selection_sha256"]
    summary = saved.descriptor["summary"]
    if not isinstance(digest, str) or not isinstance(summary, dict):
        raise ValueError("native price selection requires its digest and summary")
    coverage = summary["coverage_status"]
    if coverage not in {"complete", "partial"}:
        raise ValueError("invalid native price coverage")
    source = NativeL1SourceRef(
        kind="native_l1",
        source_id=f"native-l1-{digest[:24]}",
        sha256=digest,
        namespace="jquants-price",
        key=INPUT_PREFIX + "selections/" + digest + ".json",
    )
    build = export_dataset_snapshot(
        dataset=JQUANTS_DAILY_BARS,
        source_snapshot=source_snapshot,
        native_source=source,
        native_coverage_status=cast(CoverageStatus, coverage),
        object_date_grain="day",
        mirror_root=mirror_root,
        producer_git_commit=producer_git_commit,
        build_id=build_id,
        created_at=created_at,
    )
    return PriceExport(
        build,
        len(days),
        cast(int, summary["unavailable_days"]),
        cast(int, summary["partial_days"]),
        cast(int, summary["incomplete_days"]),
    )
