"""Current Market writes and explicitly pinned reads have different admissions."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.storage.snapshots import writer
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, dataset_manifest_key
from baibai_engine.storage.snapshots.models import LakeReleaseSourceRef, canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_write_cli import main

METRICS = "edinet.metrics"
PORTFOLIO = ("jquants.daily_bars", "jquants.market_calendar")
AT = datetime(2026, 1, 6, tzinfo=UTC)
DAY = "2026-01-05"


def _publish_fixture(tmp_path, monkeypatch, *, archived):
    datasets = (*PORTFOLIO, METRICS)
    narrow_release_policy(monkeypatch, datasets=datasets)
    source = market_store(
        tmp_path / "source.sqlite", daily_bars=(("1301", DAY, 100, 1000),), short_sale_reports=()
    )
    current = LAKE_DATASETS[METRICS]
    metrics = max(current.legacy_read_contracts, key=lambda item: item.contract_version)
    catalog = {name: LAKE_DATASETS[name] for name in datasets}
    version = SQLITE_SCHEMA_VERSION
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_metrics(asof_date,ticker,fcf_ttm) VALUES(?,'1301',7)", (DAY,)
        )
        connection.execute("INSERT INTO jquants_market_calendar VALUES(?,'1')", (DAY,))
        add_source_coverage(
            connection,
            source="jquants_market_calendar",
            coverage_key="complete-calendar",
            min_date=DAY,
            max_date=DAY,
            record_count=1,
        )
        if archived:
            catalog[METRICS] = metrics
            version -= 1
            connection.execute(f"PRAGMA user_version={version}")
            names = {column.name for column in metrics.columns}
            for column in current.columns:
                if column.name not in names:
                    connection.execute(f'ALTER TABLE edinet_metrics DROP COLUMN "{column.name}"')
    mirror = tmp_path / "mirror"
    with monkeypatch.context() as exporter:
        exporter.setattr(writer, "SQLITE_SCHEMA_VERSION", version)
        with writer.sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as sealed:
            manifests = {
                name: writer.export_dataset_snapshot(
                    dataset=dataset,
                    source_snapshot=sealed,
                    mirror_root=mirror,
                    producer_git_commit="a" * 40,
                    created_at=AT,
                )
                for name, dataset in catalog.items()
            }
    path, release = create_release(
        catalog=catalog,
        policy=release_policy_for_profile("production"),
        dataset_manifest_paths=[report.manifest_path for report in manifests.values()],
        mirror_root=mirror,
        created_at=AT,
        release_id="wire-fixture",
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=sha256_file(path),
    )
    current_path = mirror / current_release_pointer_key()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_bytes(canonical_lake_model_bytes(pointer))
    return mirror, pointer, manifests


def _target(path, pointer):
    with closing(open_connection(path)) as connection, connection:
        write_lake_store_origin(
            connection, LakeStoreOrigin(pointer.release_id, pointer.manifest_sha256)
        )
    return path


def _command(mode, mirror, target, *options):
    return main([mode, "--mirror", str(mirror), "--store", str(target), *options])


def _pinned(pointer):
    return ("--release", pointer.release_id, "--manifest-sha256", pointer.manifest_sha256)


def _metrics(path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute("SELECT ticker,fcf_ttm FROM edinet_metrics").fetchall(),
        )


@pytest.mark.parametrize("archived", [True, False], ids=["archived", "current"])
@pytest.mark.parametrize(
    ("mode", "transfer", "checks"),
    [
        ("hydrate", False, 3),
        ("hydrate-for-daily", False, 3),
        ("dehydrate", False, 2),
        ("dehydrate", True, 2),
    ],
)
def test_implicit_market_requires_current_wire_before_mutation(
    tmp_path, monkeypatch, capsys, archived, mode, transfer, checks
):
    mirror, pointer, manifests = _publish_fixture(tmp_path, monkeypatch, archived=archived)
    working = _target(tmp_path / "market.sqlite", pointer)
    if mode == "dehydrate":
        assert _command("hydrate", mirror, working, *_pinned(pointer)) == 0
    target = working
    options = ()
    if transfer:
        target = tmp_path / "incoming.sqlite"
        target.write_bytes(b"previous transfer")
        options = ("--source", str(working))
    before, source_before = target.read_bytes(), working.read_bytes()
    capsys.readouterr()
    read = LocalMirrorSource.read_bytes
    reads = Counter()

    def traced(source, key):
        reads[key] += 1
        return read(source, key)

    monkeypatch.setattr(LocalMirrorSource, "read_bytes", traced)
    result = _command(mode, mirror, target, *options)
    output = capsys.readouterr()
    keys = {
        current_release_pointer_key(),
        pointer.manifest_key,
        *(
            dataset_manifest_key(dataset=name, build_id=report.manifest.build_id)
            for name, report in manifests.items()
        ),
    }
    if archived:
        assert result == 1
        assert output.out == ""
        assert "current EDINET metrics contract" in output.err
        assert target.read_bytes() == before
        assert working.read_bytes() == source_before
        assert reads == dict.fromkeys(keys, 1)
    else:
        assert result == 0
        assert json.loads(output.out)["release_id"] == pointer.release_id
        assert _metrics(target) == (
            SQLITE_SCHEMA_VERSION,
            [] if mode == "dehydrate" else [("1301", 7)],
        )
        assert read_lake_store_origin(target) == LakeStoreOrigin(
            pointer.release_id, pointer.manifest_sha256
        )
        assert reads == dict.fromkeys(keys, checks)
        if transfer:
            assert working.read_bytes() == source_before


@pytest.mark.parametrize("typed", [False, True])
def test_explicit_archived_market_hydrate_and_compact_use_the_exact_identity(
    tmp_path, monkeypatch, capsys, typed
):
    mirror, pointer, manifests = _publish_fixture(tmp_path, monkeypatch, archived=True)
    (mirror / current_release_pointer_key()).write_bytes(b"intentionally unreadable current")
    target = _target(tmp_path / "local-copy.sqlite", pointer)
    pin = _pinned(pointer)
    if typed:
        ref = tmp_path / "release-ref.json"
        ref.write_bytes(
            canonical_lake_model_bytes(
                LakeReleaseSourceRef(
                    kind="lake_release",
                    source_id=pointer.release_id,
                    key=pointer.manifest_key,
                    sha256=pointer.manifest_sha256,
                    manifest_version=2,
                )
            )
        )
        pin = ("--release-ref", str(ref))
    obj = manifests[METRICS].manifest.partitions[0].objects[0]
    assert _command("hydrate", mirror, target, *pin) == 0
    assert json.loads(capsys.readouterr().out)["release_manifest_sha256"] == pointer.manifest_sha256
    assert _metrics(target) == (SQLITE_SCHEMA_VERSION, [("1301", 7)])
    assert _command("dehydrate", mirror, target, *pin) == 0
    assert _metrics(target) == (SQLITE_SCHEMA_VERSION, [])
    assert sha256_file(mirror / obj.key) == obj.sha256
    assert sha256_file(mirror / pointer.manifest_key) == pointer.manifest_sha256


def test_portfolio_current_does_not_need_archived_edinet_data(tmp_path, monkeypatch, capsys):
    mirror, pointer, manifests = _publish_fixture(tmp_path, monkeypatch, archived=True)
    target = _target(tmp_path / "portfolio.sqlite", pointer)
    obj = manifests[METRICS].manifest.partitions[0].objects[0]
    (mirror / obj.key).unlink()
    capsys.readouterr()
    assert _command("hydrate-portfolio", mirror, target) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rows"] == dict.fromkeys(PORTFOLIO, 1)
    assert report["release_manifest_sha256"] == pointer.manifest_sha256
    assert _metrics(target) == (SQLITE_SCHEMA_VERSION, [])
