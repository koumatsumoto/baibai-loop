"""Build synthetic market release graphs and SQLite-backed export fixtures.

`stored_release_source` supplies checksum-consistent placeholder object bytes for
reference/retention tests, not readable Parquet. The `lake` fixture instead exports
actual Parquet from a small SQLite store for row-level reader tests.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_engine.storage.lake import models
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.keys import (
    canonical_object_key,
    current_release_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
)
from baibai_engine.storage.lake.models import (
    DatasetManifest,
    LakeObject,
    LakeReleaseSourceRef,
    ManifestTotals,
    PartitionManifest,
    ReleaseDataset,
    ReleaseManifest,
    SQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.lake.release import LakeReleasePointer, create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.coverage import record_range_source_coverage
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection
from tests.helpers.lake_policy import narrow_release_policy


def release_source(release_id: str = "20260130T000000Z-release") -> LakeReleaseSourceRef:
    return LakeReleaseSourceRef(
        kind="lake_release",
        source_id=release_id,
        key=release_manifest_key(release_id=release_id),
        sha256="b" * 64,
        manifest_version=2,
    )


def _snapshot_source() -> SQLiteSnapshotSourceRef:
    digest = hashlib.sha256(b"synthetic lake test input\n").hexdigest()
    return SQLiteSnapshotSourceRef(
        kind="sqlite_snapshot",
        source_id=f"market-v{SQLITE_SCHEMA_VERSION}-{digest[:24]}",
        sha256=digest,
        schema_version=SQLITE_SCHEMA_VERSION,
        captured_at=datetime(2026, 1, 31, tzinfo=UTC),
    )


def stored_release_source(root: Path) -> tuple[Path, LakeReleaseSourceRef]:
    """A checksum-consistent retained graph, with placeholder rather than Parquet bytes.

    This exercises reference closure and retention. It does not establish Parquet
    schema validity, row content, or production release-policy eligibility.
    """

    reference = release_source()
    dataset = LAKE_DATASETS["jquants.daily_bars"]
    body = b"parquet-bytes-for-test"
    object_key = canonical_object_key(
        layer=dataset.layer,
        dataset=dataset.name,
        contract_version=dataset.contract_version,
        partition_values={"year": 2026, "month": 1},
        content_sha256=hashlib.sha256(body).hexdigest(),
        partition_by=dataset.partition_by,
    )
    object_path = root / object_key
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(body)
    lake_object = LakeObject(
        key=object_key,
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        rows=1,
        min_key=("1301", "2026-01-05"),
        max_key=("1301", "2026-01-05"),
    )
    partition = PartitionManifest(
        values={"year": 2026, "month": 1},
        objects=(lake_object,),
        sources=(_snapshot_source(),),
    )
    dataset_manifest = DatasetManifest(
        manifest_version=2,
        dataset=dataset.name,
        layer=dataset.layer,
        contract_version=dataset.contract_version,
        build_id="20260130T000000Z-legacy-abcdef01-0123456789ab",
        created_at=datetime(2026, 1, 30, tzinfo=UTC),
        producer_git_commit="1" * 40,
        partition_by=dataset.partition_by,
        partitions=(partition,),
        totals=ManifestTotals(objects=1, bytes=len(body), rows=1),
        coverage_status="partial",
        coverage_start=date(2026, 1, 1),
        data_as_of=date(2026, 1, 31),
        population_count=1,
        sources=(),
    )
    dataset_bytes = canonical_lake_model_bytes(dataset_manifest)
    dataset_path = root / dataset_manifest_key(
        dataset=dataset.name, build_id=dataset_manifest.build_id
    )
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_bytes(dataset_bytes)
    release = ReleaseManifest(
        manifest_version=2,
        release_id=reference.source_id,
        profile="production",
        created_at=datetime(2026, 1, 30, tzinfo=UTC),
        data_as_of=date(2026, 1, 31),
        datasets={
            dataset.name: ReleaseDataset(
                build_id=dataset_manifest.build_id,
                layer=dataset.layer,
                contract_version=dataset.contract_version,
                manifest_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
                data_as_of=dataset_manifest.data_as_of,
                coverage_status=dataset_manifest.coverage_status,
                totals=dataset_manifest.totals,
            )
        },
    )
    payload = canonical_lake_model_bytes(release)
    stored = root / reference.key
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    return stored, reference.model_copy(update={"sha256": hashlib.sha256(payload).hexdigest()})


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
    """Seed the three tables an L1 fixture release is built from.

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
    policy = models.PRODUCTION_RELEASE_POLICY
    monkeypatch.setattr(
        models,
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
                dataset_name=name,
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
                dataset_name=name,
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
