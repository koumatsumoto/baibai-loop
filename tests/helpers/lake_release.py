"""SQLite-backed market export fixtures with real Parquet for reader tests."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_engine.storage.snapshots.keys import (
    current_release_pointer_key,
    release_manifest_key,
)
from baibai_engine.storage.snapshots.models import (
    ReleaseDataset,
    ReleaseManifest,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.coverage import record_range_source_coverage
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import policy as snapshot_policy
from baibai_jobs.publication.datasets import (
    LAKE_DATASETS,
)
from baibai_jobs.publication.policy import release_policy_for_profile
from tests.helpers.lake_policy import narrow_release_policy

_DEFAULT_DAILY_BARS: tuple[tuple[str, str, float, float], ...] = (
    ("1301", "2026-01-05", 100.0, 1000.0),
    ("7203", "2026-01-05", 200.0, 2000.0),
    ("1301", "2026-01-20", 105.0, 1100.0),
    ("1301", "2026-02-02", 110.0, 1200.0),
)
_DEFAULT_SHORT_SALE_REPORTS: tuple[
    tuple[str, int, str, str, str, float | None, int | None, int | None], ...
] = (
    ("2026-01-06", 0, "2026-01-05", "7203", "Fund A", 0.006, 600, 6),
    ("2026-02-03", 0, "2026-02-02", "6758", "Fund B", None, None, None),
)


def market_store(
    path: Path,
    *,
    daily_bars: Sequence[tuple[str, str, float, float]] = _DEFAULT_DAILY_BARS,
    short_sale_reports: Sequence[
        tuple[str, int, str, str, str, float | None, int | None, int | None]
    ] = _DEFAULT_SHORT_SALE_REPORTS,
    short_sale_coverage: tuple[str, str, str, str, int] = (
        "test:pilot",
        "2026-01-01",
        "2026-02-28",
        "2026-03-01T00:00:00+00:00",
        2,
    ),
) -> Path:
    """Seed the three tables a lake fixture release is built from.

    The column lists live here so a schema change reaches every lake fixture at once;
    the rows stay with the caller, because how many periods a store spans is what each
    of these tests is actually about.
    """

    connection = open_connection(path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) VALUES (?, ?, ?, ?)",
        list(daily_bars),
    )
    # This synthetic fixture represents a completed fetch across the supplied span.
    # Production coverage is recorded by acquisition, never inferred from rows.
    if daily_bars:
        record_range_source_coverage(
            connection,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=date.fromisoformat(min(row[1] for row in daily_bars)),
            requested_end=date.fromisoformat(max(row[1] for row in daily_bars)),
            status="ok",
            error=None,
        )
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name,
             short_ratio, short_shares, short_trading_units
           ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?)""",
        list(short_sale_reports),
    )
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', ?, ?, ?, ?, ?, 'ok', NULL)""",
        short_sale_coverage,
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def lake(tmp_path, monkeypatch):
    datasets = ("jquants.daily_bars", "jquants.short_sale_reports", "jquants.market_calendar")
    narrow_release_policy(monkeypatch, datasets=datasets)
    # A narrow fixture permits partial calendar coverage to exercise consumer reporting.
    policy = snapshot_policy.PRODUCTION_RELEASE_POLICY
    monkeypatch.setattr(
        snapshot_policy,
        "PRODUCTION_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(
                        update={
                            "require_complete_coverage": False,
                        }
                    )
                    if item.dataset == "jquants.market_calendar"
                    else item
                    for item in policy.datasets
                ),
            }
        ),
    )
    database = market_store(tmp_path / "market.sqlite")
    connection = open_connection(database)
    connection.executemany(
        "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
        [("2025-01-05", 0), ("2026-01-05", 1), ("2026-01-20", 1)],
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    built = datetime(2026, 3, 1, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=database, mirror_root=mirror, snapshot_id="g0-test"
    ) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id="test-" + name.replace(".", "-"),
                created_at=built,
            ).manifest_path
            for name in datasets
        ]
    path, release = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="test-release",
        created_at=built,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    current = mirror / current_release_pointer_key()
    current.parent.mkdir(parents=True)
    current.write_bytes(canonical_lake_model_bytes(pointer))
    return mirror, pointer


def publish_market_fixture(path: Path, release: str, *, created_at: datetime) -> None:
    """Pin real financial/master/price Parquet for synthetic read and integration cases."""
    mirror = path.parent / "mirror"
    dataset_tables = {
        "jquants.fin_summaries": "jquants_fin_summaries",
        "jquants.master_snapshots": "jquants_master_snapshots",
        "jquants.daily_bars": "jquants_daily_bars",
        "edinet.documents": "edinet_documents",
        "edinet.document_lists": "edinet_document_lists",
        "edinet.source_archives": "edinet_source_archives",
    }
    with closing(sqlite3.connect(path)) as connection:
        present = [
            name
            for name, table in dataset_tables.items()
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ]
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        reports = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=release + "-" + name.replace(".", "-"),
                created_at=created_at.astimezone(UTC),
            )
            for name in present
        ]
    # A pinned fixture release needs no current/production policy substitution.
    manifest = ReleaseManifest(
        manifest_version=2,
        release_id=release,
        profile="production",
        created_at=created_at.astimezone(UTC),
        data_as_of=min(report.manifest.data_as_of for report in reports),
        datasets={
            report.manifest.dataset: ReleaseDataset(
                build_id=report.manifest.build_id,
                layer=report.manifest.layer,
                contract_version=report.manifest.contract_version,
                manifest_sha256=hashlib.sha256(report.manifest_path.read_bytes()).hexdigest(),
                data_as_of=report.manifest.data_as_of,
                coverage_status=report.manifest.coverage_status,
                totals=report.manifest.totals,
            )
            for report in reports
        },
    )
    payload = canonical_lake_model_bytes(manifest)
    target = mirror / release_manifest_key(release_id=release)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    with closing(sqlite3.connect(path)) as connection, connection:
        write_lake_store_origin(
            connection, LakeStoreOrigin(release, hashlib.sha256(payload).hexdigest())
        )
