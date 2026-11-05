"""Publish the master projection already rebuilt for this execution's fixed seal."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import cast

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import INPUT_PREFIX
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    MASTER_SOURCE,
    MASTER_VALIDATION_ERROR_PREFIX,
)
from baibai_engine.machine_analysis.observations.stocks.snapshot_contracts import (
    JQUANTS_MASTER_SNAPSHOTS,
)
from baibai_engine.storage.snapshots.models import CoverageStatus, NativeL1SourceRef
from baibai_engine.storage.snapshots.writer import (
    LakeBuildReport,
    SealedSQLiteSnapshot,
    export_dataset_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.integration.master_native import saved_interval


def export_master_from_native_input(
    *,
    source_snapshot: SealedSQLiteSnapshot,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """Use the same seal as models; do not reread native packs or legacy originals."""
    saved_interval(saved)
    assert saved.descriptor is not None
    digest, summary = saved.descriptor["selection_sha256"], saved.descriptor["summary"]
    if not isinstance(digest, str) or not isinstance(summary, dict):
        raise ValueError("native master selection requires its digest and summary")
    coverage = summary["coverage_status"]
    if coverage not in {"complete", "partial"}:
        raise ValueError("invalid native master coverage")
    with closing(connect_read_only(source_snapshot.path)) as connection:
        unavailable_dates = tuple(
            date.fromisoformat(str(day))
            for (day,) in connection.execute(
                "SELECT DISTINCT coverage_start FROM source_coverage WHERE source=? "
                "AND coverage_start=coverage_end AND status='unavailable' "
                "AND error LIKE ? ORDER BY coverage_start",
                (MASTER_SOURCE, MASTER_VALIDATION_ERROR_PREFIX + "%"),
            )
        )
    return export_dataset_snapshot(
        dataset=JQUANTS_MASTER_SNAPSHOTS,
        source_snapshot=source_snapshot,
        native_source=NativeL1SourceRef(
            kind="native_l1",
            source_id=f"native-l1-{digest[:24]}",
            sha256=digest,
            namespace="jquants-master",
            key=INPUT_PREFIX + "selections/" + digest + ".json",
        ),
        native_coverage_status=cast(CoverageStatus, coverage),
        object_date_grain="day",
        unavailable_dates=unavailable_dates,
        mirror_root=mirror_root,
        producer_git_commit=producer_git_commit,
        build_id=build_id,
        created_at=created_at,
    )
