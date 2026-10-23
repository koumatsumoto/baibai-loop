from __future__ import annotations

import gzip
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tools.diagnostics.benchmark_l1_export import benchmark

from baibai_engine.market.jquants_writer import store_jquants_fin_summaries
from baibai_engine.storage.lake import identity as identity_module, writer as writer_module
from baibai_engine.storage.lake.datasets import require_lake_dataset
from baibai_engine.storage.lake.immutable import install_immutable_bytes
from baibai_engine.storage.lake.keys import canonical_object_key, current_release_pointer_key
from baibai_engine.storage.lake.models import canonical_lake_model_bytes
from baibai_engine.storage.lake.objects import sha256_bytes as _sha256_bytes
from baibai_engine.storage.lake.release import LakeReleasePointer, create_release
from baibai_engine.storage.lake.retention import LakeRetentionError, apply_gc, plan_gc
from baibai_engine.storage.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_dataset_snapshot,
    export_market_store,
    sealed_sqlite_snapshot,
    validate_snapshot_parity,
)
from baibai_engine.storage.observations.coverage import record_range_source_coverage
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import open_connection

_COMMIT = "a" * 40


def _export(*, sqlite_path: Path, mirror_root: Path, **kwargs: object) -> LakeBuildReport:
    """One whole export operation: seal the store, export from it, release the seal."""
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root) as snapshot:
        return export_dataset_snapshot(
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
    assert (first.partitions, first.new_objects) == (2, 2)
    # Every partition was derived again; content addressing landed each on its key.
    assert (second.partitions, second.new_objects) == (2, 0)
    validate_snapshot_parity(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        manifest=second.manifest,
    )


def test_every_run_derives_every_partition_and_only_moved_bytes_make_new_objects(
    tmp_path: Path,
) -> None:
    """There is no base to carry from: a correction to one month costs the export of
    the whole history, and buys a manifest proved against SQLite in full each run."""

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    first = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="base-build",
    )
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close = 111.0 WHERE traded_at = '2026-02-02'"
        )
    second = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="corrected-build",
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
    assert second.partitions == 2
    assert second.new_objects == 1
    validate_snapshot_parity(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        manifest=second.manifest,
    )


def test_case_sensitive_like_keeps_partition_rows_order_and_identity(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    dataset = require_lake_dataset("jquants.daily_bars")
    uri = f"{sqlite_path.resolve().as_uri()}?mode=ro&immutable=1"
    with (
        closing(sqlite3.connect(uri, uri=True)) as legacy,
        writer_module._open_immutable(sqlite_path) as indexed,
    ):
        legacy.execute("PRAGMA case_sensitive_like=OFF")
        for period in ((2026, 1), (2026, 2)):
            legacy_rows = list(writer_module._period_rows(legacy, dataset, period))
            indexed_rows = list(writer_module._period_rows(indexed, dataset, period))
            assert indexed_rows == legacy_rows


def test_indexed_like_keeps_partition_object_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    indexed_open = writer_module._open_immutable

    @contextmanager
    def legacy_open(path: Path) -> Iterator[sqlite3.Connection]:
        with closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        ) as connection:
            connection.execute("PRAGMA case_sensitive_like=OFF")
            yield connection

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

    assert [(item.values, item.objects[0].key) for item in indexed.manifest.partitions] == [
        (item.values, item.objects[0].key) for item in legacy.manifest.partitions
    ]


def test_blob_date_still_fails_closed_with_indexed_like(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
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
    validate_snapshot_parity(
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
            export_dataset_snapshot(
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
    path, release = create_release(
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
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
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
        create_release(
            dataset_manifest_paths=[daily, short_sale],
            mirror_root=mirror,
            release_id="mixed-release",
            created_at=created_at,
        )


def test_short_sale_unknown_coverage_is_not_publishable(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
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


def test_daily_bar_lake_coverage_requires_the_short_missing_fetch(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    start, gap_start, gap_end, end = (
        date(2026, 1, 5),
        date(2026, 1, 16),
        date(2026, 1, 18),
        date(2026, 2, 2),
    )
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        connection.execute("DELETE FROM source_coverage WHERE source = 'jquants_daily_bars'")
        for low, high in ((start, date(2026, 1, 15)), (date(2026, 1, 19), end)):
            record_range_source_coverage(
                connection,
                source="jquants_daily_bars",
                operation="get_daily_quotes",
                table="jquants_daily_bars",
                date_column="traded_at",
                requested_start=low,
                requested_end=high,
                status="ok",
                error=None,
            )
    before = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=tmp_path / "mirror",
        producer_git_commit=_COMMIT,
        build_id="gap-before",
    )
    assert before.manifest.totals.rows == 4
    assert before.manifest.coverage_status == "partial"

    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        record_range_source_coverage(
            connection,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=gap_start,
            requested_end=gap_end,
            status="ok",
            error=None,
        )
    after = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=tmp_path / "mirror",
        producer_git_commit=_COMMIT,
        build_id="gap-after",
    )
    assert after.manifest.totals.rows == 4
    assert after.manifest.coverage_status == "complete"


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
            export_dataset_snapshot(
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

    report = export_market_store(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    manifests = [item.manifest_path for item in report.datasets.values()]
    _, release = create_release(
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
        with closing(sqlite3.connect(sqlite_path)) as connection, connection:
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close) "
                "VALUES ('9984', '2026-02-03', 333.0)"
            )

        report = export_dataset_snapshot(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot=snapshot,
            build_id="fixed-before-commit",
        )

        assert report.manifest.totals.rows == 4
        validate_snapshot_parity(
            sqlite_path=snapshot.path,
            mirror_root=mirror,
            manifest=report.manifest,
        )


def test_parity_rejects_a_missing_source_month(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
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
            validate_snapshot_parity(
                sqlite_path=snapshot.path,
                mirror_root=mirror,
                manifest=incomplete,
            )


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


def test_immutable_metadata_link_failure_leaves_final_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"

    def fail_link(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected link failure")

    monkeypatch.setattr("baibai_engine.storage.lake.immutable.os.link", fail_link)
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
        stdout=f"{identity_module.source_repo_root()}\n",
        stderr="",
    )
    dirty_responses = iter((top_level, dirty))
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *args, **kwargs: next(dirty_responses),
    )
    with pytest.raises(RuntimeError, match="clean tracked worktree"):
        identity_module.verified_git_commit()

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
        identity_module.verified_git_commit()

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
        identity_module.verified_git_commit()


def test_l1_manifest_digest_is_part_of_the_gc_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch)
    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_market_store(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    release_path, release = create_release(
        dataset_manifest_paths=[item.manifest_path for item in report.datasets.values()],
        mirror_root=mirror,
        release_id="gc-digest-release",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=release_path.relative_to(mirror).as_posix(),
        manifest_sha256="0" * 64,
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))

    plan = plan_gc(mirror, now=datetime(2027, 2, 4, tzinfo=UTC))

    assert pointer.manifest_key in plan.unresolved_roots
    assert pointer.manifest_key not in plan.reachable


def test_l1_export_benchmark_records_full_export_and_correction_transfer(tmp_path: Path) -> None:
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
    source_before = sqlite_path.read_bytes()

    report = benchmark(
        sqlite_path=sqlite_path,
        report_path=report_path,
        producer_commit=_COMMIT,
    )

    assert report["status"] == "measured"
    assert report["kind"] == "l1_full_history_export_benchmark"
    assert report["full"]["datasets"]["jquants.daily_bars"]["rows"] == 4  # type: ignore[index]
    assert report["correction"]["new_object_count"] == 1  # type: ignore[index]
    assert report_path.is_file()
    assert report["producer_git_commit"] == _COMMIT
    assert sqlite_path.read_bytes() == source_before


def test_the_sealed_store_is_gone_when_the_export_operation_ends(tmp_path: Path) -> None:
    """A seal is the size of the whole legacy store and one is taken per operation.

    Keeping it would make the lake grow with the number of runs rather than with what it
    publishes, so the operation that took it is what ends it — and the manifests still
    state which generation they read.
    """

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"

    report = export_market_store(
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


@pytest.mark.parametrize("fault", [None, "object_missing", "object_changed", "dataset_changed"])
def test_gc_tracks_current_release_content_without_sealed_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    """The sealed generation is named, not kept, so nothing reports it as a lost root."""
    narrow_release_policy(monkeypatch)

    sqlite_path = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    report = export_market_store(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=None,
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    release_path, release = create_release(
        dataset_manifest_paths=[item.manifest_path for item in report.datasets.values()],
        mirror_root=mirror,
        release_id="build-input-release",
        created_at=datetime(2026, 2, 4, tzinfo=UTC),
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(
        canonical_lake_model_bytes(
            LakeReleasePointer(
                release_id=release.release_id,
                manifest_key=release_path.relative_to(mirror).as_posix(),
                manifest_sha256=_sha256_bytes(release_path.read_bytes()),
            )
        )
    )

    damaged = None
    if fault is not None:
        damaged = next(iter(report.datasets.values())).manifest_path
        if fault.startswith("object_"):
            damaged = next((mirror / "lake/l2/processed").rglob("*.parquet"))
        if fault == "object_missing":
            damaged.unlink()
        elif fault == "object_changed":
            data = damaged.read_bytes()
            damaged.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        else:
            damaged.write_bytes(damaged.read_bytes() + b" ")
    plan = plan_gc(mirror, now=datetime(2027, 2, 4, tzinfo=UTC))

    if damaged is None:
        assert plan.unresolved_roots == ()
        assert plan.candidates == ()
    else:
        assert damaged.relative_to(mirror).as_posix() in plan.unresolved_roots
        with pytest.raises(LakeRetentionError, match="unresolved"):
            apply_gc(mirror, plan, plan_hash=plan.plan_hash)


def test_gc_keeps_source_and_processed_objects_in_current_market_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ("jquants.source_pages", "jquants.fin_summaries", "jquants.daily_bars")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    sqlite_path = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_source_pages("
            "request_id, page_index, source, query_json, received_at_utc, received_on, body_gzip"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "gc-source",
                0,
                "financial_summaries",
                '{"date":"2026-01-05"}',
                "2026-01-05T01:00:00+00:00",
                "2026-01-05",
                gzip.compress(b"{}"),
            ),
        )
    store_jquants_fin_summaries(
        sqlite_path,
        [{"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "gc-source"}],
        requested_start=date(2026, 1, 5),
        requested_end=date(2026, 1, 5),
    )
    mirror = tmp_path / "mirror"
    built = datetime(2026, 3, 1, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror) as snapshot:
        exports = {
            name: export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit=_COMMIT,
                build_id=f"gc-{name.replace('.', '-')}",
                created_at=built,
            )
            for name in names
        }
    release_path, release = create_release(
        dataset_manifest_paths=[item.manifest_path for item in exports.values()],
        mirror_root=mirror,
        release_id="gc-mixed-release",
        created_at=built,
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(
        canonical_lake_model_bytes(
            LakeReleasePointer(
                release_id=release.release_id,
                manifest_key=release_path.relative_to(mirror).as_posix(),
                manifest_sha256=_sha256_bytes(release_path.read_bytes()),
            )
        )
    )

    plan = plan_gc(mirror, now=datetime.now(UTC) + timedelta(days=40))
    objects = {
        name: {obj.key for part in item.manifest.partitions for obj in part.objects}
        for name, item in exports.items()
    }
    assert all(objects.values())
    assert all(key.startswith("lake/l1/source/") for key in objects["jquants.source_pages"])
    assert all(key.startswith("lake/l2/processed/") for key in objects["jquants.fin_summaries"])
    assert all(key.startswith("lake/l2/processed/") for key in objects["jquants.daily_bars"])
    assert set.union(*objects.values()) <= set(plan.reachable)
    assert plan.unresolved_roots == ()
    assert plan.candidates == ()


def test_jquants_source_and_adopted_rows_have_distinct_lake_layers(tmp_path: Path) -> None:
    sqlite_path = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_source_pages("
            "request_id, page_index, source, query_json, received_at_utc, received_on, body_gzip"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "raw-master",
                0,
                "master",
                '{"date":"2026-01-05"}',
                "2026-01-05T01:00:00+00:00",
                "2026-01-05",
                gzip.compress(b"{}"),
            ),
        )
        connection.execute(
            "INSERT INTO jquants_master_snapshots(snapshot_date, security_code, record_json) "
            "VALUES ('2026-01-05', '13010', '{\"Code\":\"13010\"}')"
        )
        connection.execute(
            "INSERT INTO jquants_fin_summaries("
            "ticker, disclosed_at, disclosure_number, document_payload"
            ") VALUES ('1301', '2026-01-05', '1', '{\"Code\":\"13010\"}')"
        )

    mirror = tmp_path / "mirror"
    for name, layer, version in (
        ("jquants.source_pages", "l1_source", 1),
        ("jquants.master_snapshots", "l2_processed", 3),
        ("jquants.fin_summaries", "l2_processed", 5),
    ):
        export = _export(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            build_id=f"layer-{name.replace('.', '-')}",
        )
        assert export.manifest.layer == layer
        assert export.manifest.contract_version == version
        assert (
            export.manifest_path.relative_to(mirror)
            .as_posix()
            .startswith(f"lake/manifests/datasets/{name}/")
        )
        for partition in export.manifest.partitions:
            for obj in partition.objects:
                assert obj.key.startswith(
                    f"lake/{'l1/source' if layer == 'l1_source' else 'l2/processed'}/"
                    f"{name}/contract=v{version}/"
                )
                metadata = pq.read_schema(mirror / obj.key).metadata
                assert metadata is not None
                assert metadata[b"baibai.layer"] == layer.encode()
                assert metadata[b"baibai.contract_version"] == str(version).encode()


def _unreferenced_processed_object(mirror: Path) -> tuple[str, Path]:
    dataset = require_lake_dataset("jquants.daily_bars")
    body = b"unreferenced processed object"
    key = canonical_object_key(
        layer=dataset.layer,
        dataset=dataset.name,
        contract_version=dataset.contract_version,
        partition_values={"year": 2026, "month": 1},
        partition_by=dataset.partition_by,
        content_sha256=_sha256_bytes(body),
    )
    path = mirror / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return key, path


def test_gc_without_pointer_marks_processed_only_mirror_unresolved(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key, path = _unreferenced_processed_object(mirror)
    plan = plan_gc(mirror, now=datetime.now(UTC) + timedelta(days=40))

    assert plan.roots == ()
    assert plan.unresolved_roots == (current_release_pointer_key(),)
    assert key in {candidate.key for candidate in plan.candidates}
    with pytest.raises(LakeRetentionError, match="root is unresolved"):
        apply_gc(mirror, plan, plan_hash=plan.plan_hash)
    assert path.read_bytes() == b"unreferenced processed object"


def test_gc_classifies_old_unreferenced_processed_object_after_grace(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key, path = _unreferenced_processed_object(mirror)
    start = datetime.now(UTC)

    before = plan_gc(mirror, now=start + timedelta(days=29))
    after = plan_gc(mirror, now=start + timedelta(days=31))

    assert before.candidates == ()
    assert len(after.candidates) == 1
    assert after.candidates[0].key == key
    assert after.candidates[0].reason == "unreferenced_lake_object"
    assert after.candidates[0].age_days >= 30
    assert path.is_file()


@pytest.mark.parametrize("failure", ["corrupt", "schema"])
def test_sealed_snapshot_rejects_invalid_store(tmp_path, failure):
    source = market_store(tmp_path / "market.sqlite")
    if failure == "corrupt":
        source.write_bytes(b"not SQLite")
    else:
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("PRAGMA user_version=1")
    with (
        pytest.raises(LakeBuildError),
        sealed_sqlite_snapshot(sqlite_path=source, mirror_root=tmp_path / "mirror"),
    ):
        pytest.fail("invalid snapshot admitted")


@pytest.mark.parametrize("failure", ["parquet", "inventory"])
def test_final_parity_failure_keeps_previous_release(tmp_path, monkeypatch, failure):
    from baibai_engine.storage.lake.keys import dataset_manifest_key

    narrow_release_policy(monkeypatch, datasets=("jquants.daily_bars",))
    source = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    baseline = _export(
        dataset_name="jquants.daily_bars",
        sqlite_path=source,
        mirror_root=mirror,
        producer_git_commit=_COMMIT,
        build_id="baseline",
    )
    release_path, release = create_release(
        dataset_manifest_paths=[baseline.manifest_path],
        mirror_root=mirror,
        release_id="previous",
        created_at=datetime.now(UTC),
    )
    previous = release_path.read_bytes()
    objects = {
        mirror / obj.key: (mirror / obj.key).read_bytes()
        for part in baseline.manifest.partitions
        for obj in part.objects
    }
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=close+1")
    promote = writer_module._promote_partitions

    def damage_final(root, built):
        result = promote(root, built)
        if failure == "parquet":
            (root / built[0].manifest.objects[0].key).write_bytes(b"broken final parquet")
        return result

    monkeypatch.setattr(writer_module, "_promote_partitions", damage_final)
    if failure == "inventory":
        parity = writer_module.validate_snapshot_parity

        def incomplete_inventory(*, manifest, **kwargs):
            parity(
                manifest=manifest.model_copy(update={"partitions": manifest.partitions[:-1]}),
                **kwargs,
            )

        monkeypatch.setattr(writer_module, "validate_snapshot_parity", incomplete_inventory)
    with pytest.raises(LakeBuildError, match=r"checksum|inventories"):
        _export(
            dataset_name="jquants.daily_bars",
            sqlite_path=source,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            build_id="failed",
        )
    assert not (
        mirror / dataset_manifest_key(dataset="jquants.daily_bars", build_id="failed")
    ).exists()
    assert release_path.read_bytes() == previous
    assert release.datasets["jquants.daily_bars"].build_id == "baseline"
    for path, payload in objects.items():
        assert path.read_bytes() == payload


@pytest.mark.parametrize("failure", ["space", "schema", "capture", "interrupted"])
def test_sealed_snapshot_cleans_capture_failures_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = tmp_path / "market.sqlite"
    source.write_bytes(b"unchanged source")
    mirror = tmp_path / "mirror"
    workspace = mirror / "lake/staging/failed-capture"

    def capture(_source: Path, destination: Path) -> int:
        destination.write_bytes(b"incomplete temporary copy")
        if failure == "capture":
            raise OSError("synthetic capture failure")
        if failure == "interrupted":
            raise KeyboardInterrupt("synthetic interruption")
        return writer_module.SQLITE_SCHEMA_VERSION - 1

    monkeypatch.setattr(writer_module, "create_snapshot", capture)
    if failure == "space":
        monkeypatch.setattr(writer_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    error = KeyboardInterrupt if failure == "interrupted" else LakeBuildError
    message = {
        "space": "insufficient disk space",
        "schema": "schema",
        "capture": "capture failed",
        "interrupted": "synthetic interruption",
    }[failure]
    with (
        pytest.raises(error, match=message),
        sealed_sqlite_snapshot(
            sqlite_path=source, mirror_root=mirror, snapshot_id="failed-capture"
        ),
    ):
        pytest.fail("failed capture exposed a snapshot")
    assert not workspace.exists()
    assert source.read_bytes() == b"unchanged source"


def test_gc_does_not_follow_staging_symlink_into_unowned_file(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    retained = mirror / "operator-data.bin"
    retained.write_bytes(b"not a lake object")
    alias = mirror / "lake" / "staging" / "old-run" / "alias.bin"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(retained)

    plan = plan_gc(mirror, now=datetime.now(UTC) + timedelta(days=40))
    assert plan.candidates == ()
    assert apply_gc(mirror, plan, plan_hash=plan.plan_hash) == ()
    assert retained.read_bytes() == b"not a lake object"
    assert alias.is_symlink()
