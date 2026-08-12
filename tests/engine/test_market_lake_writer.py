from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_engine.market.lake.raw import RawArchiveError, RawRetentionClass, archive_raw_file
from baibai_engine.market.lake.release import create_l1_release
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    export_legacy_sqlite,
    plan_affected_months,
    validate_legacy_parity,
)
from baibai_engine.market.sqlite import open_connection

_COMMIT = "a" * 40


def _market_store(path: Path) -> Path:
    connection = open_connection(path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) VALUES (?, ?, ?, ?)",
        [
            ("1301", "2026-01-05", 100.0, 1000.0),
            ("7203", "2026-01-05", 200.0, 2000.0),
            ("1301", "2026-02-02", 110.0, 1200.0),
        ],
    )
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name,
             short_ratio, short_shares, short_trading_units, is_cancellation
           ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)""",
        [
            ("2026-01-06", 0, "2026-01-05", "7203", "Fund A", 0.006, 600, 6, 0),
            ("2026-02-03", 0, "2026-02-02", "6758", "Fund B", None, None, None, 1),
        ],
    )
    connection.commit()
    connection.close()
    return path


def test_legacy_export_is_byte_deterministic_and_reuses_unchanged_objects(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="build-first",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    second = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="build-second",
        created_at=datetime(2026, 2, 5, tzinfo=UTC),
    )

    first_objects = [item.objects[0] for item in first.manifest.partitions]
    second_objects = [item.objects[0] for item in second.manifest.partitions]
    assert [(item.key, item.sha256) for item in first_objects] == [
        (item.key, item.sha256) for item in second_objects
    ]
    assert first.created_objects == 2
    assert second.created_objects == 0
    validate_legacy_parity(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        manifest=second.manifest,
    )


def test_incremental_export_replaces_only_the_affected_month(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="base-build",
    )
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close = 111.0 WHERE traded_at = '2026-02-02'"
        )
    second = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        base_manifest_path=first.manifest_path,
        build_id="incremental-build",
    )

    before = {
        (int(item.values["year"]), int(item.values["month"])): item.objects[0].key
        for item in first.manifest.partitions
    }
    after = {
        (int(item.values["year"]), int(item.values["month"])): item.objects[0].key
        for item in second.manifest.partitions
    }
    assert before[(2026, 1)] == after[(2026, 1)]
    assert before[(2026, 2)] != after[(2026, 2)]
    assert second.created_objects == 1
    assert second.changed_partitions == ("2026-02",)
    validate_legacy_parity(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        manifest=second.manifest,
    )


def test_automatic_plan_detects_fact_and_coverage_changes(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="plan-base",
    )
    assert (
        plan_affected_months(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            base_manifest_path=first.manifest_path,
        ).affected_months
        == ()
    )

    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close = 111.0 WHERE traded_at = '2026-02-02'"
        )
        connection.execute(
            """INSERT INTO source_coverage(
                 source, coverage_key, coverage_start, coverage_end,
                 fetched_at_utc, record_count, status, error
               ) VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL)""",
            (
                "jquants_daily_bars",
                "test:2026-01-01..2026-01-31",
                "2026-01-01",
                "2026-01-31",
                "2026-03-01T00:00:00+00:00",
                2,
            ),
        )

    assert plan_affected_months(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        base_manifest_path=first.manifest_path,
    ).affected_months == ((2026, 1), (2026, 2))


def test_incremental_export_preserves_partition_lineage(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) "
            "VALUES ('7203', '2026-02-02', 222.0)"
        )
    mirror = tmp_path / "mirror"
    first = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        source_ingest_ids=("ingest-a",),
        build_id="lineage-base",
    )
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close = 111.0 WHERE traded_at = '2026-02-02'"
        )
    second = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        source_ingest_ids=("ingest-b",),
        base_manifest_path=first.manifest_path,
        build_id="lineage-next",
    )
    lineage = {
        (int(item.values["year"]), int(item.values["month"])): item.source_ingest_ids
        for item in second.manifest.partitions
    }
    assert lineage == {
        (2026, 1): ("ingest-a",),
        (2026, 2): ("ingest-a", "ingest-b"),
    }
    assert second.manifest.source_ingest_ids == ("ingest-a", "ingest-b")


def test_incremental_export_rejects_a_different_transform(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="transform-base",
    )
    payload = json.loads(first.manifest_path.read_text())
    payload["transform_fingerprint"] = f"sha256:{'f' * 64}"
    changed = tmp_path / "different-transform.json"
    changed.write_text(json.dumps(payload))

    with pytest.raises(LakeBuildError, match="full rebuild"):
        export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            base_manifest_path=changed,
            build_id="transform-next",
        )


def test_short_sale_export_preserves_pk_values_and_cancellation(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    result = export_legacy_sqlite(
        dataset_name="jquants.short_sale_reports",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="short-build",
    )

    assert result.manifest.totals.rows == 2
    validate_legacy_parity(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        manifest=result.manifest,
    )


def test_raw_archive_is_append_only_and_strips_endpoint_query(tmp_path) -> None:
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"original-provider-bytes")
    mirror = tmp_path / "mirror"
    target, _, metadata = archive_raw_file(
        source_path=source,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="ingest-1",
        suffix="json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 2, 4, tzinfo=UTC),
        endpoint="https://api.jquants.com/v2/equities/bars/daily?token=secret",
    )
    assert target.read_bytes() == source.read_bytes()
    assert metadata.endpoint == "https://api.jquants.com/v2/equities/bars/daily"

    source.write_bytes(b"different")
    with pytest.raises(RawArchiveError, match="different bytes"):
        archive_raw_file(
            source_path=source,
            mirror_root=mirror,
            provider="jquants",
            dataset="jquants.daily_bars",
            ingest_id="ingest-1",
            suffix="json.gz",
            retention_class=RawRetentionClass.PRESERVE,
            retrieved_at=datetime(2026, 2, 4, tzinfo=UTC),
        )


def test_release_manifest_composes_exact_dataset_builds(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    manifests = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            build_id=f"build-{index}",
        ).manifest_path
        for index, name in enumerate(("jquants.daily_bars", "jquants.short_sale_reports"), start=1)
    ]
    path, release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="release-1",
    )
    assert path.is_file()
    assert set(release.datasets) == {
        "jquants.daily_bars",
        "jquants.short_sale_reports",
    }
