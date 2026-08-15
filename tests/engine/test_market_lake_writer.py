from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tools.diagnostics.benchmark_l1_export import benchmark

from baibai_engine.market.lake import identity as identity_module
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake import write_cli as write_cli_module
from baibai_engine.market.lake import writer as writer_module
from baibai_engine.market.lake.datasets import require_pilot_dataset
from baibai_engine.market.lake.immutable import install_immutable_bytes
from baibai_engine.market.lake.keys import current_l1_pointer_key
from baibai_engine.market.lake.models import canonical_lake_model_bytes
from baibai_engine.market.lake.objects import sha256_bytes as _sha256_bytes
from baibai_engine.market.lake.raw import (
    RawArchiveError,
    RawRetentionClass,
    archive_raw_file,
    raw_source_ref,
)
from baibai_engine.market.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.market.lake.retention import apply_gc, plan_gc
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_legacy_sqlite,
    export_pilot_legacy,
    plan_affected_months,
    sealed_sqlite_snapshot,
    validate_legacy_parity,
)
from baibai_engine.market.sqlite import open_connection

_COMMIT = "a" * 40


@pytest.fixture(autouse=True)
def _small_pilot_release_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
            }
        )
        for item in lake_models.PILOT_RELEASE_POLICY.datasets
    )
    monkeypatch.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        lake_models.PILOT_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
    )


def _market_store(path: Path) -> Path:
    connection = open_connection(path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) VALUES (?, ?, ?, ?)",
        [
            ("1301", "2026-01-05", 100.0, 1000.0),
            ("7203", "2026-01-05", 200.0, 2000.0),
            ("1301", "2026-01-20", 105.0, 1100.0),
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
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-02-28', '2026-03-01T00:00:00+00:00', 2, 'ok', NULL)"""
    )
    connection.commit()
    connection.close()
    return path


def _export(*, sqlite_path: Path, mirror_root: Path, **kwargs: object) -> LakeBuildReport:
    """One whole export operation: seal the store, export from it, release the seal."""
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root) as snapshot:
        return export_legacy_sqlite(
            mirror_root=mirror_root,
            source_snapshot=snapshot,
            **kwargs,  # type: ignore[arg-type]
        )


def test_legacy_export_is_byte_deterministic_and_reuses_unchanged_objects(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="build-first",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    second = _export(
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
    first = _export(
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
    second = _export(
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
    first = _export(
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
    raw_a = tmp_path / "raw-a.json.gz"
    raw_a.write_bytes(b"raw-a")
    _, metadata_a, _ = archive_raw_file(
        source_path=raw_a,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="ingest-a",
        suffix=".json.gz",
        retention_class=RawRetentionClass.BUFFER,
        retrieved_at=datetime(2026, 2, 4, tzinfo=UTC),
        request_start=date(2026, 1, 1),
        request_end=date(2026, 2, 28),
    )
    first = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        raw_source_refs=(raw_source_ref(metadata_a),),
        build_id="lineage-base",
    )
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close = 111.0 WHERE traded_at = '2026-02-02'"
        )
    raw_b = tmp_path / "raw-b.json.gz"
    raw_b.write_bytes(b"raw-b")
    _, metadata_b, _ = archive_raw_file(
        source_path=raw_b,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="ingest-b",
        suffix=".json.gz",
        retention_class=RawRetentionClass.BUFFER,
        retrieved_at=datetime(2026, 2, 5, tzinfo=UTC),
        request_start=date(2026, 2, 1),
        request_end=date(2026, 2, 28),
    )
    second = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        raw_source_refs=(raw_source_ref(metadata_b),),
        base_manifest_path=first.manifest_path,
        build_id="lineage-next",
    )
    lineage = {
        (int(item.values["year"]), int(item.values["month"])): tuple(
            source.source_id for source in item.sources if source.kind == "raw_ingest"
        )
        for item in second.manifest.partitions
    }
    assert lineage == {
        (2026, 1): ("ingest-a",),
        (2026, 2): ("ingest-a", "ingest-b"),
    }
    assert second.manifest.sources == ()


def test_export_rejects_raw_lineage_outside_target_dataset_or_partition(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    raw = tmp_path / "raw.json.gz"
    raw.write_bytes(b"raw")
    _, wrong_dataset_metadata, _ = archive_raw_file(
        source_path=raw,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.short_sale_reports",
        ingest_id="wrong-dataset",
        suffix=".json.gz",
        retention_class=RawRetentionClass.BUFFER,
        retrieved_at=datetime(2026, 2, 5, tzinfo=UTC),
        request_start=date(2026, 1, 1),
        request_end=date(2026, 2, 28),
    )
    with pytest.raises(LakeBuildError, match="does not match target dataset"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            raw_source_refs=(raw_source_ref(wrong_dataset_metadata),),
            build_id="wrong-dataset-build",
        )

    _, wrong_range_metadata, _ = archive_raw_file(
        source_path=raw,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="wrong-range",
        suffix=".json.gz",
        retention_class=RawRetentionClass.BUFFER,
        retrieved_at=datetime(2025, 12, 5, tzinfo=UTC),
        request_start=date(2025, 12, 1),
        request_end=date(2025, 12, 31),
    )
    with pytest.raises(LakeBuildError, match="does not cover any rebuilt partition"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            raw_source_refs=(raw_source_ref(wrong_range_metadata),),
            build_id="wrong-range-build",
        )

    valid = raw_source_ref(wrong_range_metadata).model_copy(update={"provider": "other"})
    with pytest.raises(ValueError, match="metadata identity does not match"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            raw_source_refs=(valid,),
            build_id="wrong-provider-build",
        )


def test_incremental_export_rejects_a_different_transform(tmp_path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = _export(
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
        _export(
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
    result = _export(
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
    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id="release-generation"
    ) as snapshot:
        manifests = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
                build_id=f"build-{index}",
                created_at=datetime(2026, 2, 4, tzinfo=UTC),
            ).manifest_path
            for index, name in enumerate(
                ("jquants.daily_bars", "jquants.short_sale_reports"), start=1
            )
        ]
    path, release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="release-1",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    assert path.is_file()
    assert set(release.datasets) == {
        "jquants.daily_bars",
        "jquants.short_sale_reports",
    }


def test_release_rejects_mixed_sqlite_snapshot_generations(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    created_at = datetime(2026, 8, 14, tzinfo=UTC)
    daily = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="mixed-1",
        created_at=created_at,
    ).manifest_path
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) "
            "VALUES ('9984', '2026-02-03', 333.0)"
        )
    short_sale = _export(
        dataset_name="jquants.short_sale_reports",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="mixed-2",
        created_at=created_at,
    ).manifest_path

    with pytest.raises(ValueError, match="share one SQLite snapshot generation"):
        create_l1_release(
            dataset_manifest_paths=[daily, short_sale],
            mirror_root=mirror,
            release_id="mixed-release",
            created_at=created_at,
        )


def test_short_sale_unknown_coverage_is_not_publishable(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "DELETE FROM source_coverage WHERE source = ?", ("jquants_short_sale_reports",)
        )
    report = _export(
        dataset_name="jquants.short_sale_reports",
        sqlite_path=sqlite_path,
        mirror_root=tmp_path / "mirror",
        producer_git_commit=_COMMIT,
        build_id="unknown-coverage",
    )

    assert report.manifest.coverage_status == "partial"


def test_wal_snapshot_is_one_generation_for_both_pilot_datasets(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    live = sqlite3.connect(sqlite_path)
    live.execute("PRAGMA journal_mode = WAL")
    live.execute("PRAGMA wal_autocheckpoint = 0")
    live.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) "
        "VALUES ('9984', '2026-02-03', 333.0)"
    )
    live.commit()
    mirror = tmp_path / "mirror"

    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id="wal-generation"
    ) as snapshot:
        reports = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
                build_id=f"wal-{index}",
            )
            for index, name in enumerate(
                ("jquants.daily_bars", "jquants.short_sale_reports"), start=1
            )
        ]
    live.close()

    assert reports[0].manifest.totals.rows == 5
    assert {
        source.sha256
        for report in reports
        for partition in report.manifest.partitions
        for source in partition.sources
        if source.kind == "sqlite_snapshot"
    } == {snapshot.ref.sha256}


def test_pilot_orchestration_exports_one_release_generation(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    report = export_pilot_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    manifests = [item.manifest_path for item in report.datasets.values()]
    _, release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="pilot-orchestration",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )

    assert set(release.datasets) == set(report.datasets)
    assert {
        source.sha256
        for item in report.datasets.values()
        for partition in item.manifest.partitions
        for source in partition.sources
        if source.kind == "sqlite_snapshot"
    } == {report.snapshot.sha256}


def test_commit_after_snapshot_does_not_change_export_generation(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id="before-later-commit"
    ) as snapshot:
        with sqlite3.connect(sqlite_path) as connection:
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close) "
                "VALUES ('9984', '2026-02-03', 333.0)"
            )

        report = export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot=snapshot,
            build_id="fixed-before-commit",
        )

        assert report.manifest.totals.rows == 4
        validate_legacy_parity(
            sqlite_path=snapshot.path,
            mirror_root=mirror,
            manifest=report.manifest,
        )


def test_parity_rejects_a_missing_source_month(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror) as snapshot:
        report = export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot=snapshot,
            build_id="complete-months",
        )
        incomplete = report.manifest.model_copy(
            update={"partitions": report.manifest.partitions[:-1]}
        )

        with pytest.raises(LakeBuildError, match="month inventories differ"):
            validate_legacy_parity(
                sqlite_path=snapshot.path,
                mirror_root=mirror,
                manifest=incomplete,
            )


def test_invalid_build_id_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    with pytest.raises(ValueError, match="path-safe"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            build_id="../escape",
        )

    assert not [path for path in mirror.rglob("*") if path.is_file()]
    assert not (tmp_path / "escape").exists()


def test_transform_source_digest_change_rejects_partition_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    base = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="code-base",
    )
    original = writer_module.sha256_file

    def changed_digest(path: Path) -> str:
        if path.name == "writer.py":
            return "f" * 64
        return original(path)

    monkeypatch.setattr(writer_module, "sha256_file", changed_digest)
    with pytest.raises(LakeBuildError, match="full rebuild"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            base_manifest_path=base.manifest_path,
            build_id="code-next",
        )


def test_immutable_metadata_link_failure_leaves_final_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"

    def fail_link(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected link failure")

    monkeypatch.setattr("baibai_engine.market.lake.immutable.os.link", fail_link)
    with pytest.raises(OSError, match="injected"):
        install_immutable_bytes(target, b"{}\n")

    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_git_identity_rejects_dirty_or_unknown_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirty = subprocess.CompletedProcess(
        args=("git", "status"),
        returncode=0,
        stdout=" M writer.py\n",
        stderr="",
    )
    top_level = subprocess.CompletedProcess(
        args=("git", "rev-parse"),
        returncode=0,
        stdout=f"{write_cli_module._source_repo_root()}\n",
        stderr="",
    )
    dirty_responses = iter((top_level, dirty))
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *args, **kwargs: next(dirty_responses),
    )
    with pytest.raises(RuntimeError, match="clean tracked worktree"):
        write_cli_module._git_commit()

    responses = iter(
        (
            top_level,
            subprocess.CompletedProcess(args=("git", "status"), returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=("git", "rev-parse"), returncode=0, stdout=f"{'0' * 40}\n", stderr=""
            ),
        )
    )
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *args, **kwargs: next(responses),
    )
    with pytest.raises(RuntimeError, match="verifiable git commit"):
        write_cli_module._git_commit()

    other_repo = subprocess.CompletedProcess(
        args=("git", "rev-parse"),
        returncode=0,
        stdout=f"{Path('/tmp/different-source-checkout')}\n",
        stderr="",
    )
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *args, **kwargs: other_repo,
    )
    with pytest.raises(RuntimeError, match="source repository identity is ambiguous"):
        write_cli_module._git_commit()


def test_l1_manifest_digest_is_part_of_the_gc_root(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_pilot_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    release_path, release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in report.datasets.values()],
        mirror_root=mirror,
        release_id="gc-digest-release",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=release_path.relative_to(mirror).as_posix(),
        manifest_sha256="0" * 64,
    )
    pointer_path = mirror / current_l1_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))

    plan = plan_gc(mirror, now=datetime(2027, 2, 4, tzinfo=UTC))

    assert pointer.manifest_key in plan.unresolved_roots
    assert pointer.manifest_key not in plan.reachable


def test_expired_unreferenced_buffer_raw_uses_the_two_sweep_delete_gate(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    raw = tmp_path / "raw.json.gz"
    raw.write_bytes(b"buffered raw")
    object_path, metadata_path, _metadata = archive_raw_file(
        source_path=raw,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="expired-buffer",
        suffix=".json.gz",
        retention_class=RawRetentionClass.BUFFER,
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_start=date(2025, 12, 1),
        request_end=date(2025, 12, 31),
    )
    first = plan_gc(mirror, now=datetime(2026, 4, 2, tzinfo=UTC))

    assert {item.key for item in first.candidates} == {
        object_path.relative_to(mirror).as_posix(),
        metadata_path.relative_to(mirror).as_posix(),
    }
    assert apply_gc(mirror, first, plan_hash=first.plan_hash) == ()
    second = plan_gc(mirror, now=first.evaluated_at + timedelta(days=8))
    assert set(apply_gc(mirror, second, plan_hash=second.plan_hash)) == {
        object_path.relative_to(mirror).as_posix(),
        metadata_path.relative_to(mirror).as_posix(),
    }
    assert not object_path.exists()
    assert not metadata_path.exists()


def test_preserve_raw_never_enters_gc_candidates(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    raw = tmp_path / "raw.json.gz"
    raw.write_bytes(b"preserved raw")
    object_path, metadata_path, _metadata = archive_raw_file(
        source_path=raw,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="preserved-raw",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2020, 1, 1, tzinfo=UTC),
        request_start=date(2019, 12, 1),
        request_end=date(2019, 12, 31),
    )

    plan = plan_gc(mirror, now=datetime(2027, 1, 1, tzinfo=UTC))

    candidate_keys = {item.key for item in plan.candidates}
    assert object_path.relative_to(mirror).as_posix() not in candidate_keys
    assert metadata_path.relative_to(mirror).as_posix() not in candidate_keys


def test_l1_export_benchmark_records_full_and_incremental_transfer(tmp_path: Path) -> None:
    sqlite_path = _market_store(tmp_path / "market.sqlite")
    report_path = tmp_path / "report.json"

    report = benchmark(
        sqlite_path=sqlite_path,
        report_path=report_path,
        producer_commit=_COMMIT,
    )

    assert report["status"] == "passed"
    assert report["full"]["datasets"]["jquants.daily_bars"]["rows"] == 4  # type: ignore[index]
    assert report["incremental_correction"]["new_object_count"] == 1  # type: ignore[index]
    assert report_path.is_file()


class TestTransformIdentity:
    """What an L1 build has to be identified by before a release may carry it."""

    def test_coverage_semantics_are_part_of_the_transform_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coverage decides which months a build touches and whether it calls itself
        complete, so a build made under other coverage rules is not the same transform
        even when every Parquet byte matches."""

        dataset = require_pilot_dataset("jquants.daily_bars")
        baseline = writer_module._transform_fingerprint(dataset)
        target = Path(writer_module.__file__).resolve().parents[1] / "sqlite" / "coverage.py"
        assert target.is_file()
        real = writer_module.sha256_file
        monkeypatch.setattr(
            writer_module,
            "sha256_file",
            lambda path: "0" * 64 if path == target else real(path),
        )

        assert writer_module._transform_fingerprint(dataset) != baseline


def test_the_sealed_store_is_gone_when_the_export_operation_ends(tmp_path: Path) -> None:
    """A seal is the size of the whole legacy store and one is taken per operation.

    Keeping it would make the lake grow with the number of runs rather than with what it
    publishes, so the operation that took it is what ends it — and the manifests still
    state which generation they read.
    """

    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    report = export_pilot_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )

    assert not list((mirror / "lake" / "staging").rglob("*.sqlite"))
    assert not any(path.name.endswith(".sqlite") for path in mirror.rglob("*") if path.is_file())
    assert {
        source.source_id
        for item in report.datasets.values()
        for partition in item.manifest.partitions
        for source in partition.sources
        if source.kind == "sqlite_snapshot"
    } == {report.snapshot.source_id}


def test_a_seal_a_killed_operation_left_behind_is_collected(tmp_path: Path) -> None:
    """A process killed mid-export cannot run its own cleanup, so collection must.

    Nothing reaches into the staging prefix from a manifest, so the only thing that can
    reclaim it is age — and without that the leftover is the whole legacy store sitting
    where no capacity figure derived from manifests would ever account for it.
    """

    mirror = tmp_path / "mirror"
    abandoned = mirror / "lake" / "staging" / "killed-operation" / "snapshot.sqlite"
    abandoned.parent.mkdir(parents=True)
    abandoned.write_bytes(b"sealed legacy store")
    key = abandoned.relative_to(mirror).as_posix()

    first = plan_gc(mirror, now=datetime.now(UTC) + timedelta(days=8))

    assert {item.key for item in first.candidates} == {key}
    assert apply_gc(mirror, first, plan_hash=first.plan_hash) == ()
    second = plan_gc(mirror, now=first.evaluated_at + timedelta(days=8))
    assert apply_gc(mirror, second, plan_hash=second.plan_hash) == (key,)
    assert not abandoned.exists()


def test_a_release_stays_resolvable_with_no_sealed_store_to_reach(tmp_path: Path) -> None:
    """The sealed generation is named, not kept, so nothing reports it as a lost root."""

    sqlite_path = _market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_pilot_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    release_path, release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in report.datasets.values()],
        mirror_root=mirror,
        release_id="build-input-release",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    pointer_path = mirror / current_l1_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(
        canonical_lake_model_bytes(
            L1ReleasePointer(
                release_id=release.release_id,
                manifest_key=release_path.relative_to(mirror).as_posix(),
                manifest_sha256=_sha256_bytes(release_path.read_bytes()),
            )
        )
    )

    plan = plan_gc(mirror, now=datetime(2027, 2, 4, tzinfo=UTC))

    assert plan.unresolved_roots == ()
    assert plan.candidates == ()
