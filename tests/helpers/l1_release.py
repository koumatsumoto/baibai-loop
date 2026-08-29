"""Build a minimal but real L1 release inside a mirror, closure and all.

Resolving an `L1ReleaseSourceRef` walks what it roots — dataset manifests and every
Parquet object they name — so a stand-in payload no longer stands in. Tests that need
a resolvable release build one here rather than each inventing a shape production
never produces.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.keys import dataset_manifest_key, release_manifest_key
from baibai_engine.market.lake.models import (
    DatasetManifest,
    L1ReleaseSourceRef,
    LakeObject,
    ManifestTotals,
    PartitionManifest,
    ReleaseDataset,
    ReleaseManifest,
    SQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
)
from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION


def release_source(release_id: str = "20260130T000000Z-release") -> L1ReleaseSourceRef:
    return L1ReleaseSourceRef(
        kind="l1_release",
        source_id=release_id,
        key=release_manifest_key(release_id=release_id),
        sha256="b" * 64,
        manifest_version=1,
    )


def _snapshot_source() -> SQLiteSnapshotSourceRef:
    digest = hashlib.sha256(b"synthetic L1 test input\n").hexdigest()
    return SQLiteSnapshotSourceRef(
        kind="sqlite_snapshot",
        source_id=f"market-v{SQLITE_SCHEMA_VERSION}-{digest[:24]}",
        sha256=digest,
        schema_version=SQLITE_SCHEMA_VERSION,
        captured_at=datetime(2026, 1, 31, tzinfo=UTC),
    )


def stored_release_source(root: Path) -> tuple[Path, L1ReleaseSourceRef]:
    """A retained source whose whole closure is on disk, and the manifest file it names.

    A stand-in payload was enough while resolving one meant hashing one file. Resolving
    an L1 release now walks what it roots — dataset manifests and every Parquet object
    they name — so the fixture has to be a release the mirror actually holds, or the
    test would be asserting against a shape production never sees.
    """

    reference = release_source()
    dataset = LAKE_DATASETS["jquants.daily_bars"]
    body = b"parquet-bytes-for-test"
    object_key = (
        f"lake/l1/canonical/{dataset.name}/contract=v{dataset.contract_version}"
        f"/year=2026/month=1/part-{hashlib.sha256(body).hexdigest()}.parquet"
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
        manifest_version=1,
        dataset=dataset.name,
        layer="l1_canonical",
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
        manifest_version=1,
        release_id=reference.source_id,
        profile="production",
        created_at=datetime(2026, 1, 30, tzinfo=UTC),
        data_as_of=date(2026, 1, 31),
        datasets={
            dataset.name: ReleaseDataset(
                build_id=dataset_manifest.build_id,
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
    tuple[str, int, str, str, str, float | None, int | None, int | None, int], ...
] = (
    ("2026-01-06", 0, "2026-01-05", "7203", "Fund A", 0.006, 600, 6, 0),
    ("2026-02-03", 0, "2026-02-02", "6758", "Fund B", None, None, None, 1),
)


def market_store(
    path: Path,
    *,
    daily_bars: Sequence[tuple[str, str, float, float]] = _DEFAULT_DAILY_BARS,
    short_sale_reports: Sequence[
        tuple[str, int, str, str, str, float | None, int | None, int | None, int]
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
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name,
             short_ratio, short_shares, short_trading_units, is_cancellation
           ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)""",
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
