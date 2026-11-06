"""Finite import keeps all original identities, storage days, and requested scope."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.helpers.lake_release import publish_market_fixture
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY,
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, merge_scope
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.data_collection.receipts.articles.storage_contracts import ARTICLE_RECEIPTS_DDL
from baibai_engine.machine_analysis.observations.articles.storage_contracts import (
    ARTICLE_OBSERVATIONS_DDL,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.sqlite.source_cache.facts_scope_upgrade import SCHEMA49_SQL
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import edinet_archive_native as cli
from baibai_jobs.maintenance.edinet_archive_import import (
    apply_import,
    prepare_import,
    verify_import,
)

AT = datetime(2026, 11, 5, 8, tzinfo=UTC)
DAY = date(2020, 1, 1)
EMPTY_SCOPE = FactsScope()


def source(tmp_path, *, rows=None, scope=EMPTY_SCOPE):
    database = tmp_path / "legacy.sqlite"
    with closing(open_connection(database)) as connection:
        for doc, kind, day, body in (
            [("S100ONE", 1, DAY, b"first original")] if rows is None else rows
        ):
            retain_source_archive(connection, doc, kind, body, stored_on=day)
        with connection:
            merge_scope(connection, scope)
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES ('1301','2026-11-05',100)"
            )
    publish_market_fixture(database, "fixed-legacy", created_at=AT)
    origin = read_lake_store_origin(database)
    release = resolve_release(
        LocalMirrorSource(tmp_path / "mirror"),
        origin.release_id,
        manifest_sha256=origin.release_manifest_sha256,
    )
    return database, release


def stage(
    tmp_path,
    store,
    name,
    rows=(),
    *,
    scope=EMPTY_SCOPE,
    status=None,
    schema=50,
    declared=None,
    at=AT,
):
    packet = tmp_path / name
    packet.mkdir()
    database = packet / "receipts.sqlite"
    if schema in (48, 49):
        with closing(sqlite3.connect(database)) as connection:
            ddl = SCHEMA49_SQL
            if schema == 48:
                ddl = ddl.replace(ARTICLE_RECEIPTS_DDL, "").replace(ARTICLE_OBSERVATIONS_DDL, "")
            connection.executescript(ddl + f"PRAGMA user_version={schema};")
            with connection:
                connection.execute(
                    "INSERT INTO source_coverage VALUES ('edinet_research_facts','initialized',NULL,NULL,?,0,'ok',?)",
                    (AT.isoformat(), json.dumps({"since": "1990-01-01"})),
                )
    else:
        open_connection(database).close()
    with closing(sqlite3.connect(database)) as connection:
        for doc, kind, day, body in rows:
            retain_source_archive(connection, doc, kind, body, stored_on=day)
        if schema == 50:
            with connection:
                merge_scope(connection, scope)
    group = "-".join(name.split("-")[:2])
    report = {
        "source": group,
        "asof": AT.date().isoformat(),
        "started_at": at.isoformat(),
        "archives": [[row[0], row[1]] for row in rows] if declared is None else declared,
        "list_receipts": [],
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, name, packet, market=True)
    if status is not None:
        store.store_object(
            f"coordination/market-sources/{name}/integrated.json",
            json.dumps({"status": status}).encode(),
        )
    return packet


def test_all_stages_full_history_scope_and_first_day_survive_without_http(tmp_path):
    database, release = source(
        tmp_path, scope=FactsScope(date(2025, 1, 1), frozenset({"S100MISSING"}))
    )
    store = MemoryR2Store()
    body = b"shared opaque original\xff"
    stage(tmp_path, store, "edinet-list-sealed", [("S100TWO", 5, DAY, body)], status="integrated")
    stage(
        tmp_path,
        store,
        "edinet-list-late",
        [("S100ONE", 1, DAY - timedelta(days=1), b"first original")],
        at=AT - timedelta(days=1),
    )
    stage(
        tmp_path,
        store,
        "edinet-metrics-recovery",
        [("S100THREE", 1, DAY, body)],
        status="needs_explicit_recovery",
    )
    stage(
        tmp_path,
        store,
        "edinet-facts-pending",
        scope=FactsScope(date(2024, 1, 1), frozenset({"S100REQUESTED"}), frozenset({"a" * 64})),
    )
    before = {key: value.body for key, value in store.values.items()}
    original = database.read_bytes()
    directory = tmp_path / "import"
    plan = prepare_import(store, source=database, release=release, directory=directory)
    assert len(plan["databases"]) == 5
    assert any(item.get("handoff", {}).get("late_before_integrated") for item in plan["databases"])
    assert {key: value.body for key, value in store.values.items()} == before
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    result = apply_import(owner, directory=directory)
    fixed = owner.freeze()
    assert result["archives"] == 3
    assert fixed.scope == FactsScope(
        date(2024, 1, 1), frozenset({"S100MISSING", "S100REQUESTED"}), frozenset({"a" * 64})
    )
    assert fixed.metadata("S100ONE", 1).stored_on == DAY - timedelta(days=1)
    assert all(fixed.metadata(*key).received_at_utc is None for key in fixed.archive_keys)
    assert fixed.read_archive("S100THREE", 1) == fixed.read_archive("S100TWO", 5) == body
    assert (
        len(
            [
                key
                for key in store.values
                if key.startswith("l1/edinet/archives/") and key.endswith(".zip")
            ]
        )
        == 2
    )
    puts = list(store.put_keys)
    assert apply_import(owner, directory=directory) == result
    verify_import(owner, directory=directory)
    assert store.put_keys == puts
    assert database.read_bytes() == original
    assert all(store.values[key].body == value for key, value in before.items())


def test_first_zip_only_in_stage_imports_with_no_canonical_archive_dataset(tmp_path):
    database, release = source(tmp_path, rows=[])
    assert "edinet.source_archives" not in release.dataset_names()
    store = MemoryR2Store()
    stage(tmp_path, store, "edinet-facts-first", [("S100FIRST", 1, DAY, b"first")])
    directory = tmp_path / "import"
    plan = prepare_import(store, source=database, release=release, directory=directory)
    assert len(plan["archives"]) == 1
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    apply_import(owner, directory=directory)
    verify_import(owner, directory=directory)
    assert owner.freeze().read_xbrl_zip("S100FIRST") == b"first"


def test_cli_import_uses_fixed_release_and_originals_without_provider_http(tmp_path, monkeypatch):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    store.close = Mock()
    http = Mock()
    monkeypatch.setattr(cli, "Boto3R2Store", Mock(return_value=store))
    monkeypatch.setattr(cli, "EDINETHTTP", http)

    @contextmanager
    def lake(**kwargs):
        assert kwargs["bucket"] == "isolated-fixture"
        yield None, SimpleNamespace(source=LocalMirrorSource(tmp_path / "mirror"))

    monkeypatch.setattr(cli, "open_lake", lake)
    directory = tmp_path / "import"
    args = ["--bucket", "isolated-fixture", "--directory", str(directory)]
    assert (
        cli.main(
            [
                "prepare-import",
                *args,
                "--legacy-database",
                str(database),
                "--release",
                release.release_id,
                "--manifest-sha256",
                release.manifest_sha256,
            ]
        )
        == 0
    )
    assert not store.put_keys
    for command in ("initialize", "apply-import", "verify-import"):
        assert cli.main([command, *args]) == 0
    assert EdinetArchiveL1(store, directory).freeze().read_xbrl_zip("S100ONE") == b"first original"
    http.assert_not_called()


@pytest.mark.parametrize("schema", [48, 49])
def test_old_integrated_stage_uses_finite_copy_without_inventing_global_scope(tmp_path, schema):
    database, release = source(tmp_path, scope=FactsScope(date(2025, 1, 1)))
    store = MemoryR2Store()
    stage(
        tmp_path,
        store,
        "edinet-facts-old",
        [("S100OLD", 1, DAY, b"old")],
        status="integrated",
        schema=schema,
    )
    before = {key: value.body for key, value in store.values.items()}
    directory = tmp_path / "import"
    plan = prepare_import(store, source=database, release=release, directory=directory)
    assert plan["databases"][1]["path"].endswith("schema50.sqlite")
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    apply_import(owner, directory=directory)
    assert owner.freeze().scope.since == date(2025, 1, 1)
    assert owner.freeze().scope.doc_ids == frozenset({"S100OLD"})
    assert owner.freeze().scope.legacy_origins
    assert all(store.values[key].body == value for key, value in before.items())


@pytest.mark.parametrize("damage", ["missing", "extra", "conflict"])
def test_bad_or_conflicting_stage_blocks_before_any_target_write(tmp_path, damage):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    rows = [("S100ONE", 1, DAY, b"conflict" if damage == "conflict" else b"first original")]
    stage(
        tmp_path,
        store,
        "edinet-facts-bad",
        rows,
        declared=[] if damage == "extra" else [["S100MISSING", 1]] if damage == "missing" else None,
    )
    puts = list(store.put_keys)
    with pytest.raises(ValueError, match=r"declaration differs|conflicting bytes"):
        prepare_import(store, source=database, release=release, directory=tmp_path / "import")
    assert store.put_keys == puts
    assert HEAD_KEY not in store.values


@pytest.mark.parametrize("remaining", [0, 1])
def test_dehydrated_or_partially_hydrated_canonical_is_not_an_empty_migration(tmp_path, remaining):
    database, release = source(
        tmp_path, rows=[("S100ONE", 1, DAY, b"one"), ("S100TWO", 5, DAY, b"two")]
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DELETE FROM edinet_source_archives WHERE doc_id='S100TWO'")
        if not remaining:
            connection.execute("DELETE FROM edinet_source_archives")
    store = MemoryR2Store()
    with pytest.raises(ValueError, match="hydrate all"):
        prepare_import(store, source=database, release=release, directory=tmp_path / "import")
    assert not store.put_keys


@pytest.mark.parametrize("changed", ["body", "scope", "stage", "origin"])
def test_changed_prepared_input_stops_before_target_write(tmp_path, changed):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    stage(tmp_path, store, "edinet-facts-one", [("S100TWO", 1, DAY, b"stage")])
    directory = tmp_path / "import"
    plan = prepare_import(store, source=database, release=release, directory=directory)
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    path = database if changed != "stage" else Path(plan["databases"][1]["path"])
    with closing(sqlite3.connect(path)) as connection, connection:
        if changed in {"body", "stage"}:
            connection.execute("UPDATE edinet_source_archives SET zip_content=?", (b"changed",))
        elif changed == "scope":
            merge_scope(connection, FactsScope(doc_ids=frozenset({"S100NEW"})))
        else:
            connection.execute("UPDATE lake_store_origin SET release_id='changed'")
    puts = list(store.put_keys)
    # Binding changes must not be accepted even when archive contents are equal.
    expected_error = "canonical" if changed == "origin" else "changed"
    with pytest.raises(ValueError, match=expected_error):
        apply_import(owner, directory=directory)
    assert store.put_keys == puts


@pytest.mark.parametrize("changed", ["new_identity", "metadata", "scope", "pending"])
def test_unrelated_target_or_pending_work_is_not_repaired_by_import(tmp_path, changed):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    directory = tmp_path / "import"
    prepare_import(store, source=database, release=release, directory=directory)
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    item = NativeArchive.from_response(
        "S100ONE" if changed == "metadata" else "S100FOREIGN",
        1,
        EdinetResponse(AT, b"first original"),
    )
    if changed == "pending":
        put = store.put_file
        store.put_file = lambda *args, **kwargs: (_ for _ in ()).throw(
            LakePublishError("interrupted")
        )
        with pytest.raises(LakePublishError):
            owner.publish([item])
        store.put_file = put
    elif changed == "scope":
        owner.publish([], scope=FactsScope(date(1900, 1, 1)))
    else:
        owner.publish([item])
    puts = list(store.put_keys)
    with pytest.raises(ValueError, match=r"outside|metadata differs|another import"):
        apply_import(owner, directory=directory)
    assert store.put_keys == puts


@pytest.mark.parametrize("target", ["body", "head"])
def test_interrupted_transfer_resumes_only_the_same_preserved_plan(tmp_path, target):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    directory = tmp_path / "import"
    prepare_import(store, source=database, release=release, directory=directory)
    owner = EdinetArchiveL1(store, directory)
    owner.initialize()
    put = store.put_file
    failed = False

    def interrupt(key, *args, **kwargs):
        nonlocal failed
        if not failed and (key == HEAD_KEY) == (target == "head"):
            failed = True
            raise LakePublishError("interrupted transfer")
        return put(key, *args, **kwargs)

    store.put_file = interrupt
    with pytest.raises(LakePublishError):
        apply_import(owner, directory=directory)
    assert (directory / "pending.json").is_file()
    assert apply_import(owner, directory=directory)["archives"] == 1
    verify_import(owner, directory=directory)
    assert not (directory / "pending.json").exists()
