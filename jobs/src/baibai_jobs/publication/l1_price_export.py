"""Publish the price projection from the same SQLite seal used by this run."""

from __future__ import annotations

from collections.abc import Mapping
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


def export_prices_from_native_input(
    *,
    source_snapshot: SealedSQLiteSnapshot,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """Export this run's rebuilt seal; never GET/decode native originals a second time."""
    from baibai_engine.machine_analysis.generation.price_inputs import (
        INPUT_PREFIX,
        rehydrate_price_input,
    )
    from baibai_engine.storage.snapshots.models import NativeL1SourceRef

    fixed, _ = rehydrate_price_input(saved)
    digest = fixed.descriptor["selection_sha256"]
    summary = fixed.descriptor["summary"]
    if not isinstance(digest, str) or not isinstance(summary, Mapping):
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
    return export_dataset_snapshot(
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
