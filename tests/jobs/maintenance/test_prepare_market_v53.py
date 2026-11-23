from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.storage.snapshots import writer
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLiteSchemaError,
    connect_current,
    open_connection,
)
from baibai_jobs.maintenance.market import prepare_v53
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

NOW = datetime(2026, 11, 23, tzinfo=UTC)
METRICS = "edinet.metrics"
ADD = "parent_attributable_equity_json"


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    # Same v52 table shape and genuine v3 wire. Cross-code smoke rehearsals use
    # an actual old producer; the regression suite must not need Git history.
    narrow_release_policy(monkeypatch, datasets=(METRICS,), carries_history=None)
    source = tmp_path / "v52 private #? 入力.sqlite"
    mirror = tmp_path / "mirror"
    with closing(open_connection(source)) as conn, conn:
        conn.execute(f"ALTER TABLE edinet_metrics DROP COLUMN {ADD}")
        conn.execute("PRAGMA user_version=52")
        conn.execute(
            "INSERT INTO edinet_metrics"
            "(asof_date,ticker,fcf_ttm,financing_lease_repayments_cash_effect) "
            "VALUES ('2026-11-22','1234',34,-7)"
        )
        conn.execute(
            "INSERT INTO source_coverage VALUES "
            "('fixture-store-local','fixed','2026-11-22','2026-11-22',"
            "'2026-11-23T00:00:00+00:00',0,'ok',NULL)"
        )
    old = LAKE_DATASETS[METRICS].read_contract(3)
    with monkeypatch.context() as version:
        version.setattr(writer, "SQLITE_SCHEMA_VERSION", 52)
        with writer.sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as sealed:
            manifest = writer.export_dataset_snapshot(
                source_snapshot=sealed,
                mirror_root=mirror,
                dataset=old,
                build_id="old-metrics",
                created_at=NOW,
                producer_git_commit="a" * 40,
            ).manifest_path
    path, release = create_release(
        dataset_manifest_paths=[manifest],
        mirror_root=mirror,
        catalog={METRICS: old},
        policy=release_policy_for_profile("production"),
        created_at=NOW,
        release_id="exact-old",
    )
    digest = sha256_file(path)
    with closing(sqlite3.connect(source)) as conn, conn:
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, digest))
    return {
        "source": source,
        "source_sha256": sha256_file(source),
        "mirror": mirror,
        "release_id": release.release_id,
        "manifest_sha256": digest,
        "output": tmp_path / "new v53 #? 出力.sqlite",
    }


def test_new_closed_copy_keeps_old_values_and_origin_without_current_pointer(inputs, capsys):
    source, output = inputs["source"], inputs["output"]
    before = source.read_bytes()
    assert not (inputs["mirror"] / current_release_pointer_key()).exists()
    args = [
        part
        for name, value in inputs.items()
        for part in (f"--{name.replace('_', '-')}", str(value))
    ]
    assert prepare_v53.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "status": "prepared_local_market_copy",
        "schema_version": 53,
        "source_sha256": inputs["source_sha256"],
        "output_sha256": sha256_file(output),
        "release_id": inputs["release_id"],
        "manifest_sha256": inputs["manifest_sha256"],
    }
    assert source.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600
    origin = LakeStoreOrigin(inputs["release_id"], inputs["manifest_sha256"])
    assert read_lake_store_origin(output) == origin
    connection = connect_current(output)
    assert connection is not None
    with closing(connection):
        assert connection.execute(
            f"SELECT fcf_ttm, financing_lease_repayments_cash_effect,{ADD} FROM edinet_metrics"
        ).fetchall() == [(34.0, -7.0, None)]
        assert connection.execute("SELECT source,coverage_key FROM source_coverage").fetchall() == [
            ("fixture-store-local", "fixed")
        ]
    with pytest.raises(SQLiteSchemaError, match="found user_version=52, expected=53"):
        connect_current(source)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("wrong-digest", "source SHA-256 differs"),
        ("wrong-manifest", "manifest digest"),
        ("old-wrong-origin", "source Lake origin"),
        ("already-converted", "schema 52"),
        ("compact", "not fully hydrated"),
        ("wrong-shape", "schema is incomplete"),
    ],
)
def test_wrong_inputs_fail_without_creating_or_modifying_output(inputs, case, message):
    source, output = inputs["source"], inputs["output"]
    if case in {"compact", "wrong-shape", "already-converted", "old-wrong-origin"}:
        with closing(sqlite3.connect(source)) as conn, conn:
            if case == "compact":
                conn.execute("DELETE FROM edinet_metrics")
            elif case == "wrong-shape":
                conn.execute("ALTER TABLE source_coverage DROP COLUMN error")
            elif case == "already-converted":
                conn.execute("PRAGMA user_version=53")
            else:
                write_lake_store_origin(conn, LakeStoreOrigin("different", "b" * 64))
        inputs["source_sha256"] = sha256_file(source)
    if case == "wrong-digest":
        inputs["source_sha256"] = "a" * 64
    if case == "wrong-manifest":
        inputs["manifest_sha256"] = "c" * 64
    before = source.read_bytes()
    with pytest.raises((ValueError, SQLiteSchemaError, prepare_v53.LakeReadError), match=message):
        prepare_v53.prepare_market_v53_copy(**inputs)
    assert not output.exists()
    assert source.read_bytes() == before


def test_live_wal_cannot_be_silently_dropped_by_main_file_copy(inputs):
    source, output = inputs["source"], inputs["output"]
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE edinet_metrics SET fcf_ttm=95")
        writer.commit()
        wal = Path(f"{source}-wal")
        wal_before = wal.read_bytes()
        source_before = source.read_bytes()
        inputs["source_sha256"] = sha256_file(source)
        with pytest.raises(ValueError, match="sidecar"):
            prepare_v53.prepare_market_v53_copy(**inputs)
        assert wal.read_bytes() == wal_before
        assert source.read_bytes() == source_before
        assert not output.exists()
        assert writer.execute("SELECT fcf_ttm FROM edinet_metrics").fetchone() == (95.0,)


@pytest.mark.parametrize("race", [False, True])
def test_an_existing_output_never_gets_replaced(inputs, monkeypatch, race):
    output = inputs["output"]
    if race:
        link = prepare_v53.os.link

        def concurrent_output(source, target):
            output.write_bytes(b"other writer")
            link(source, target)

        monkeypatch.setattr(prepare_v53.os, "link", concurrent_output)
    else:
        output.write_bytes(b"other writer")
    with pytest.raises(FileExistsError):
        prepare_v53.prepare_market_v53_copy(**inputs)
    assert output.read_bytes() == b"other writer"
