"""Publish only the safe calendar day projection from the shared generation seal."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import cast

from baibai_engine.machine_analysis.generation.calendar_inputs import INPUT_PREFIX
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_MARKET_CALENDAR,
)
from baibai_engine.storage.snapshots.models import CoverageStatus, NativeL1SourceRef
from baibai_engine.storage.snapshots.writer import (
    LakeBuildReport,
    SealedSQLiteSnapshot,
    export_dataset_snapshot,
)
from baibai_jobs.integration.calendar_native import saved_interval


def export_calendar_from_native_input(
    *,
    source_snapshot: SealedSQLiteSnapshot,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """No raw reread or second normalization; unchanged months reuse exact objects."""
    saved_interval(saved)
    assert saved.descriptor is not None
    digest, summary = saved.descriptor["selection_sha256"], saved.descriptor["summary"]
    if not isinstance(digest, str) or not isinstance(summary, dict):
        raise ValueError("native calendar selection requires its digest and summary")
    coverage = summary.get("coverage_status")
    if coverage not in {"complete", "partial"}:
        raise ValueError("invalid native calendar coverage")
    return export_dataset_snapshot(
        dataset=JQUANTS_MARKET_CALENDAR,
        source_snapshot=source_snapshot,
        native_source=NativeL1SourceRef(
            kind="native_l1",
            source_id=f"native-l1-{digest[:24]}",
            sha256=digest,
            namespace="jquants-market-calendar",
            key=INPUT_PREFIX + "selections/" + digest + ".json",
        ),
        native_coverage_status=cast(CoverageStatus, coverage),
        object_date_grain="month",
        mirror_root=mirror_root,
        producer_git_commit=producer_git_commit,
        build_id=build_id,
        created_at=created_at,
    )
