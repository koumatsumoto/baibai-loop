"""Derive a price L2 snapshot from retained native L1 pages."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.lake_receipts import iter_retained_requests
from baibai_engine.data_collection.providers.jquants.native_index import (
    calendar_sessions,
    index_market_originals,
    plan_required_days,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    materialize_price_days,
    rebuild_price_days,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import JQUANTS_DAILY_BARS
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.models import CoverageStatus, validate_dataset_contract
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.writer import (
    LakeBuildReport,
    SealedSQLiteSnapshot,
    export_dataset_snapshot,
    export_l1_derived_dataset,
)


@dataclass(frozen=True, slots=True)
class PriceExport:
    build: LakeBuildReport
    source_days: int
    missing_days: int
    partial_days: int
    unknown_requests: int


def export_prices_from_l1(
    *,
    source: LakeBuildReport,
    mirror_root: Path,
    producer_git_commit: str,
    known_at: datetime,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> PriceExport:
    """Build full retained prices, holding normalized rows one month at a time."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    validate_dataset_contract(source.manifest, JQUANTS_SOURCE_PAGES)
    source_sha = hashlib.sha256(source.manifest_path.read_bytes()).hexdigest()
    cache = LakeObjectCache(root=mirror_root, source=LocalMirrorSource(mirror_root))
    paths = materialize_partitions(
        cache, dataset=JQUANTS_SOURCE_PAGES, partitions=source.manifest.partitions
    )
    with lake_session() as session:
        future_rows = session.connection.execute(
            "SELECT COUNT(*) FROM read_parquet($files, union_by_name = false) "
            "WHERE source = 'daily_bars' AND CAST(received_at_utc AS TIMESTAMPTZ) > $cutoff",
            {"files": [str(path) for path in paths], "cutoff": known_at},
        ).fetchone()
        if future_rows is None:
            raise ValueError("price source timestamp check failed")
        requests = list(
            iter_retained_requests(
                session.connection, paths, source="daily_bars", known_at=known_at
            )
        )
        index = index_market_originals(requests, source="daily_bars")
        days = {item.start for item in index.receipts}
        for incomplete in index.incomplete:
            query = dict(incomplete.query or ())
            if set(query) == {"date"}:
                with suppress(ValueError):
                    days.add(date.fromisoformat(query["date"]))
        if not days:
            raise ValueError("no complete native price requests are available")
        start, end = min(days), max(days)
        horizon = tuple(start + timedelta(days=i) for i in range((end - start).days + 1))
        gaps = plan_required_days(index, days=horizon, require_nonempty=False)
        possible_closures = set(gaps.days) - days
        if possible_closures:
            calendar = index_market_originals(
                iter_retained_requests(
                    session.connection, paths, source="market_calendar", known_at=known_at
                ),
                source="market_calendar",
            )
            sessions, unknown_calendar = calendar_sessions(calendar, start=start, end=end)
            if not unknown_calendar:
                closed = {day for day in possible_closures if sessions.get(day) is False}
                gaps = plan_required_days(
                    index,
                    days=(day for day in horizon if day not in closed),
                    require_nonempty=False,
                )
        grouped: dict[tuple[int, int], list[RetainedRequest]] = defaultdict(list)
        for item in requests:
            params = item.request.params if item.request else item.first_query or {}
            if set(params) == {"date"}:
                with suppress(ValueError):
                    day = date.fromisoformat(params["date"])
                    grouped[(day.year, day.month)].append(item)
        # Once grouped, release each month's native bodies after processing it;
        # the growing DuckDB projection must not coexist with all decoded history.
        del requests
        months: dict[tuple[int, int], set[date]] = defaultdict(set)
        for day in days:
            months[(day.year, day.month)].add(day)
        partial_days = 0
        for month, selected_days in sorted(months.items()):
            month_requests = grouped.pop(month, [])
            results = rebuild_price_days(month_requests, days=selected_days)
            materialize_price_days(
                session.connection,
                results.values(),
                l1_coverage_status=source.manifest.coverage_status,
                append=month != min(months),
            )
            partial_days += sum(
                item.status != "ok" or item.incomplete_requests > 0 for item in results.values()
            )
            del month_requests, results
        coverage: CoverageStatus = (
            "partial"
            if gaps.days or gaps.unknown_incomplete_requests or partial_days or future_rows[0]
            else "complete"
        )
        build = export_l1_derived_dataset(
            dataset=JQUANTS_DAILY_BARS,
            source_manifest=source.manifest,
            source_manifest_sha256=source_sha,
            connection=session.connection,
            table="daily_bars",
            coverage_status=coverage,
            mirror_root=mirror_root,
            producer_git_commit=producer_git_commit,
            build_id=build_id,
            created_at=created_at,
        )
    return PriceExport(
        build,
        len(days),
        len(gaps.days),
        partial_days,
        len(gaps.unknown_incomplete_requests) + int(future_rows[0]),
    )


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
