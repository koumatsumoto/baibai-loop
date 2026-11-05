"""Publish the exact margin-alert projection used by readers and model generation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.margin_alerts_inputs import INPUT_PREFIX
from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
    JQUANTS_MARGIN_ALERTS,
)
from baibai_engine.storage.snapshots.models import CoverageStatus, NativeL1SourceRef
from baibai_engine.storage.snapshots.writer import (
    LakeBuildReport,
    SealedSQLiteSnapshot,
    export_dataset_snapshot,
)
from baibai_jobs.integration.margin_alerts_native import saved_interval


def export_margin_alerts_from_native_input(
    *,
    source_snapshot: SealedSQLiteSnapshot,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
) -> LakeBuildReport:
    """Monthly objects reuse unchanged bytes; no raw reread or second normalization."""
    saved_interval(saved)
    assert saved.descriptor is not None
    digest, summary = saved.descriptor["selection_sha256"], saved.descriptor["summary"]
    if not isinstance(digest, str) or not isinstance(summary, dict):
        raise ValueError("native margin alerts selection requires its digest and summary")
    coverage = summary.get("coverage_status")
    if coverage not in {"complete", "partial"}:
        raise ValueError("native margin alerts selection requires projection coverage")
    return export_dataset_snapshot(
        dataset=JQUANTS_MARGIN_ALERTS,
        source_snapshot=source_snapshot,
        native_source=NativeL1SourceRef(
            kind="native_l1",
            source_id=f"native-l1-{digest[:24]}",
            sha256=digest,
            namespace="jquants-margin-alerts",
            key=INPUT_PREFIX + "selections/" + digest + ".json",
        ),
        native_coverage_status=cast(CoverageStatus, coverage),
        object_date_grain="month",
        mirror_root=mirror_root,
        producer_git_commit=producer_git_commit,
    )
