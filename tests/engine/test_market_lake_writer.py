from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tools.diagnostics.benchmark_l1_export import benchmark

from baibai_engine.market.lake import identity as identity_module
from baibai_engine.market.lake import write_cli as write_cli_module
from baibai_engine.market.lake import writer as writer_module
from baibai_engine.market.lake.datasets import require_lake_dataset
from baibai_engine.market.lake.immutable import install_immutable_bytes
from baibai_engine.market.lake.keys import current_l1_pointer_key
from baibai_engine.market.lake.models import canonical_lake_model_bytes
from baibai_engine.market.lake.objects import sha256_bytes as _sha256_bytes
from baibai_engine.market.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.market.lake.retention import apply_gc, plan_gc
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_lake_legacy,
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
    validate_legacy_parity,
)
from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.sqlite.lake_origin import LakeStoreOrigin, write_lake_store_origin

_COMMIT = "a" * 40


def _affected_periods(
    *, dataset_name: str, sqlite_path: Path, base_manifest_path: Path
) -> tuple[tuple[int, ...], ...]:
    """Ask the publisher which partitions a base manifest no longer describes.

    ``writer.affected_periods`` is the production comparison; reaching it needs the
    same manifest load and immutable open that a rebuild does, which is all this
    does.
    """

    dataset = require_lake_dataset(dataset_name)
    base = writer_module._load_base_manifest(
        base_manifest_path, dataset, transform=writer_module._transform_fingerprint(dataset)
    )
    assert base is not None
    with writer_module._open_immutable(sqlite_path) as connection:
        writer_module._validate_sqlite_contract(connection, dataset)
        return writer_module.affected_periods(connection, dataset, base)


def _export(*, sqlite_path: Path, mirror_root: Path, **kwargs: object) -> LakeBuildReport:
    """One whole export operation: seal the store, export from it, release the seal."""
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root) as snapshot:
        return export_legacy_sqlite(
            mirror_root=mirror_root,
            source_snapshot=snapshot,
            **kwargs,  # type: ignore[arg-type]
        )


def test_legacy_export_is_byte_deterministic_and_reuses_unchanged_objects(tmp_path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="plan-base",
    )
    assert (
        _affected_periods(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            base_manifest_path=first.manifest_path,
        )
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

    assert _affected_periods(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        base_manifest_path=first.manifest_path,
    ) == ((2026, 1), (2026, 2))


def test_case_sensitive_like_keeps_partition_rows_order_and_identity(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    dataset = require_lake_dataset("jquants.daily_bars")
    uri = f"{sqlite_path.resolve().as_uri()}?mode=ro&immutable=1"
    with (
        sqlite3.connect(uri, uri=True) as legacy,
        writer_module._open_immutable(sqlite_path) as indexed,
    ):
        legacy.execute("PRAGMA case_sensitive_like=OFF")
        for period in ((2026, 1), (2026, 2)):
            legacy_rows = writer_module._period_rows(legacy, dataset, period)
            indexed_rows = writer_module._period_rows(indexed, dataset, period)
            assert indexed_rows == legacy_rows
            assert writer_module._source_state_sha256(
                indexed, dataset, period, indexed_rows
            ) == writer_module._source_state_sha256(legacy, dataset, period, legacy_rows)


def test_logical_coverage_change_affects_earnings_partition_but_fetch_time_does_not(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_earnings_calendar(announcement_date, ticker) "
        "VALUES ('2026-02-10', '1301')"
    )
    connection.execute(
        "INSERT INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
        "record_count, status, error"
        ") VALUES ('jpx_earnings_calendar', 'snapshot', '2026-02-01', '2026-02-28', "
        "'2026-02-01T00:00:00+00:00', 1, 'ok', NULL)"
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    first = _export(
        dataset_name="jquants.earnings_calendar",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="earnings-before-coverage-change",
    )

    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE source_coverage SET fetched_at_utc = '2026-02-02T00:00:00+00:00' "
            "WHERE source = 'jpx_earnings_calendar'"
        )
    assert (
        _affected_periods(
            dataset_name="jquants.earnings_calendar",
            sqlite_path=sqlite_path,
            base_manifest_path=first.manifest_path,
        )
        == ()
    )

    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "UPDATE source_coverage SET status = 'failed', error = 'provider refused' "
            "WHERE source = 'jpx_earnings_calendar'"
        )
    assert _affected_periods(
        dataset_name="jquants.earnings_calendar",
        sqlite_path=sqlite_path,
        base_manifest_path=first.manifest_path,
    ) == ((2026,),)


def test_indexed_like_keeps_partition_source_hash_and_object_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    indexed_open = writer_module._open_immutable

    def legacy_open(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA case_sensitive_like=OFF")
        return connection

    monkeypatch.setattr(writer_module, "_open_immutable", legacy_open)
    legacy = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="legacy-like",
    )
    monkeypatch.setattr(writer_module, "_open_immutable", indexed_open)
    indexed = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="indexed-like",
    )

    assert [
        (item.values, item.source_state_sha256, item.objects[0].key)
        for item in indexed.manifest.partitions
    ] == [
        (item.values, item.source_state_sha256, item.objects[0].key)
        for item in legacy.manifest.partitions
    ]


def test_blob_date_still_fails_closed_with_indexed_like(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            ("9999", sqlite3.Binary(b"2026-01-31"), 1.0),
        )

    with pytest.raises(LakeBuildError):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=sqlite_path,
            mirror_root=tmp_path / "mirror",
            producer_git_commit=_COMMIT,
        )


def test_incremental_export_rejects_a_different_transform(tmp_path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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


def test_release_manifest_composes_exact_dataset_builds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    sqlite_path = market_store(tmp_path / "market.sqlite")
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


def test_release_rejects_mixed_sqlite_snapshot_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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


def test_pilot_orchestration_exports_one_release_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    report = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
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

        with pytest.raises(LakeBuildError, match="partition inventories differ"):
            validate_legacy_parity(
                sqlite_path=snapshot.path,
                mirror_root=mirror,
                manifest=incomplete,
            )


def test_a_daily_build_checks_the_months_it_wrote_and_the_month_inventory(
    tmp_path: Path,
) -> None:
    """What a build has to prove is that it is correct, not that the store still is.

    A carried object is addressed by the digest of its own bytes, so re-deriving it from
    SQLite on every run re-proves the previous build and makes a one month correction
    cost the whole history. The month inventory is still compared in full, because a
    month missing from one side is a hole no per-partition check would look at.
    """

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror) as snapshot:
        first = export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot=snapshot,
            build_id="seed-build",
        )

    read: list[str] = []
    real = writer_module.pq.read_table
    with (
        sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror) as snapshot,
        mock.patch.object(
            writer_module.pq,
            "read_table",
            side_effect=lambda path, *a, **k: (read.append(Path(path).name), real(path, *a, **k))[
                1
            ],
        ),
    ):
        second = export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot=snapshot,
            base_manifest_path=first.manifest_path,
            build_id="carry-build",
        )

    assert second.changed_partitions == ()
    # The manifest still describes the whole history; the partitions were carried.
    assert second.manifest.totals == first.manifest.totals
    assert len(second.manifest.partitions) == len(first.manifest.partitions)
    # Nothing was rebuilt, so no Parquet object had to be read back.
    assert read == []


def test_invalid_build_id_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
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
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    base = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="code-base",
    )
    original = writer_module.semantic_source_digest

    def changed_digest(path: Path) -> str:
        if path.name == "writer.py":
            return "f" * 64
        return original(path)

    monkeypatch.setattr(writer_module, "semantic_source_digest", changed_digest)
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


def test_l1_manifest_digest_is_part_of_the_gc_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
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


def test_l1_export_benchmark_records_full_and_incremental_transfer(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    connection = open_connection(sqlite_path)
    write_lake_store_origin(
        connection,
        LakeStoreOrigin(
            release_id="benchmark-baseline",
            release_manifest_sha256="b" * 64,
        ),
    )
    connection.commit()
    connection.close()
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

        dataset = require_lake_dataset("jquants.daily_bars")
        baseline = writer_module._transform_fingerprint(dataset)
        target = Path(writer_module.__file__).resolve().parents[1] / "sqlite" / "coverage.py"
        assert target.is_file()
        real = writer_module.semantic_source_digest
        monkeypatch.setattr(
            writer_module,
            "semantic_source_digest",
            lambda path: "0" * 64 if path == target else real(path),
        )

        assert writer_module._transform_fingerprint(dataset) != baseline


def test_the_sealed_store_is_gone_when_the_export_operation_ends(tmp_path: Path) -> None:
    """A seal is the size of the whole legacy store and one is taken per operation.

    Keeping it would make the lake grow with the number of runs rather than with what it
    publishes, so the operation that took it is what ends it — and the manifests still
    state which generation they read.
    """

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    report = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
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
    assert apply_gc(mirror, first, plan_hash=first.plan_hash) == (key,)
    assert not abandoned.exists()


def test_a_release_stays_resolvable_with_no_sealed_store_to_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    """The sealed generation is named, not kept, so nothing reports it as a lost root."""

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
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
