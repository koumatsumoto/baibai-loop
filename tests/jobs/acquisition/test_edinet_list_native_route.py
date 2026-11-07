from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.edinet_list_native import acquire_lists
from baibai_jobs.acquisition.inputs import export_source_input
from baibai_jobs.integration.edinet_handoff import import_handoff
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.integration.source_handoff import main as handoff_main

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture(params=["explicit", "default"])
def native_mode(monkeypatch, request):
    if request.param == "explicit":
        monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    else:
        monkeypatch.delenv("BAIBAI_EDINET_LIST_NATIVE", raising=False)


def payload(day, *, rows=()):
    return json.dumps(
        {
            "metadata": {"parameter": {"date": day.isoformat()}, "resultset": {"count": len(rows)}},
            "results": list(rows),
        },
        ensure_ascii=False,
    ).encode()


def receipt(day=DAY, *, at=AT, body=None):
    return NativeListReceipt.from_response(
        day, EdinetResponse(at, payload(day) if body is None else body)
    )


def owner_at(tmp_path, originals=()):
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path / "l1")
    owner.freeze(False)
    owner.publish(originals)
    return store, owner


class Client:
    def __init__(self, now=AT, responses=None):
        self.now = now
        self.calls = []
        self.responses = responses or {}

    def fetch_list(self, day):
        self.calls.append(day)
        body = self.responses.get(day, payload(day))
        if isinstance(body, Exception):
            raise body
        return EdinetResponse(self.now + timedelta(seconds=len(self.calls)), body)


def test_source_only_plan_retains_out_of_window_rechecks_and_real_today_order(tmp_path):
    old = DAY - timedelta(days=1000)
    old_receipt = receipt(old, at=AT - timedelta(days=1000))
    _, owner = owner_at(tmp_path, [old_receipt])
    today = DAY + timedelta(days=1)
    client = Client(AT + timedelta(days=1))
    report = acquire_lists(owner, client, start=DAY - timedelta(days=1), end=DAY, today=today)
    assert client.calls == [today, DAY, old, DAY - timedelta(days=1)]
    assert report["failed"] == []
    assert report["received_count"] == 4
    assert not (tmp_path / "market.sqlite").exists()
    assert len(tuple(owner.freeze().iter_receipts())) == 5
    # The same L1 contains the real current-before-history proof. No L2 success needed.
    client = Client(AT + timedelta(days=1, hours=1))
    acquire_lists(owner, client, start=DAY - timedelta(days=1), end=DAY, today=today)
    assert client.calls == [today, DAY]


def test_default_540_day_scope_is_not_shortened_by_missing_l2(tmp_path):
    _, owner = owner_at(tmp_path)
    client = Client()
    report = acquire_lists(owner, client, start=DAY - timedelta(days=540), end=DAY, today=DAY)
    assert len(client.calls) == 541
    assert set(client.calls) == {DAY - timedelta(days=i) for i in range(541)}
    assert report["received_count"] == 541


@pytest.mark.parametrize("failure", [b"invalid JSON \xff", EdinetRateLimitError("quota")])
def test_later_failure_preserves_successful_and_malformed_originals(tmp_path, failure):
    _, owner = owner_at(tmp_path)
    prior = DAY - timedelta(days=1)
    client = Client(responses={prior: failure})
    report = acquire_lists(owner, client, start=prior, end=prior, today=DAY)
    assert report["failed"]
    assert client.calls == [DAY, prior]
    originals = tuple(owner.freeze().iter_receipts())
    assert len(originals) == (2 if isinstance(failure, bytes) else 1)
    if isinstance(failure, bytes):
        assert originals[-1].decoded().body == failure
    assert not (tmp_path / "l1/pending.json").exists()


def test_force_history_rechecks_saved_days_without_faking_clocks(tmp_path):
    _, owner = owner_at(tmp_path)
    acquire_lists(owner, Client(), start=DAY - timedelta(days=1), end=DAY, today=DAY)
    client = Client(AT + timedelta(hours=1))
    acquire_lists(
        owner, client, start=DAY - timedelta(days=1), end=DAY, today=DAY, force_history=True
    )
    assert client.calls == [DAY, DAY - timedelta(days=1)]
    clocks = [item.received_at for item in owner.freeze().iter_receipts()]
    assert clocks[-2:] == [client.now + timedelta(seconds=1), client.now + timedelta(seconds=2)]


def contents(database):
    with closing(sqlite3.connect(database)) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in (
                "edinet_list_responses",
                "edinet_document_lists",
                "edinet_documents",
                "source_coverage",
            )
        }


@pytest.mark.parametrize("bad_kind", ["malformed", "tied"])
def test_cold_and_warm_rebuild_preserve_same_eligible_prior_and_report_failure(tmp_path, bad_kind):
    row = {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", "secCode": "20000"}
    first = receipt(body=payload(DAY, rows=[row]))
    _, owner = owner_at(tmp_path, [first])
    warm = tmp_path / "warm.sqlite"
    materialize_lists(owner.freeze(), warm)
    bad = receipt(at=AT + timedelta(seconds=1), body=b"bad JSON")
    additions = [bad]
    if bad_kind == "tied":
        additions = [
            receipt(at=bad.received_at),
            receipt(at=bad.received_at, body=payload(DAY, rows=[row])),
        ]
    fixed = owner.publish(additions)
    cold = tmp_path / "cold.sqlite"
    warm_result = materialize_lists(fixed, warm)
    cold_result = materialize_lists(fixed, cold)
    assert warm_result == cold_result
    assert warm_result["failed_days"] == [DAY.isoformat()]
    assert contents(warm) == contents(cold)
    with closing(sqlite3.connect(cold)) as conn:
        assert conn.execute(
            "SELECT selected_receipt_id,latest_received_at_utc FROM edinet_document_lists"
        ).fetchone() == (first.receipt_id, AT.isoformat(timespec="microseconds"))
        assert conn.execute("SELECT status FROM source_coverage").fetchone() == ("ok",)


def test_receipt_set_not_max_clock_drives_rebuild_and_fixed_head_is_stable(tmp_path):
    origin = {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", "secCode": "20000"}
    vanished = {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "0"}
    late = receipt(at=AT, body=payload(DAY, rows=[origin]))
    latest = receipt(at=AT + timedelta(seconds=2), body=payload(DAY, rows=[vanished]))
    _, owner = owner_at(tmp_path, [latest])
    before = owner.freeze()
    database = tmp_path / "market.sqlite"
    materialize_lists(before, database)
    changed = owner.publish([late])
    materialize_lists(changed, database)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT sec_code FROM edinet_documents").fetchone() == ("20000",)
    earlier = tmp_path / "old.sqlite"
    materialize_lists(before, earlier)
    with closing(sqlite3.connect(earlier)) as conn:
        assert conn.execute("SELECT sec_code FROM edinet_documents").fetchone() == (None,)


def test_unknown_legacy_receipt_blocks_materialization_without_dropping_anything(tmp_path):
    _, owner = owner_at(tmp_path, [receipt()])
    database = tmp_path / "market.sqlite"
    unknown = receipt(at=AT + timedelta(seconds=1))
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            (
                unknown.receipt_id,
                DAY.isoformat(),
                unknown.received_at.isoformat(timespec="microseconds"),
                unknown.body_gzip,
            ),
        )
    before = database.read_bytes()
    with pytest.raises(ValueError, match="outside the fixed"):
        materialize_lists(owner.freeze(), database)
    assert database.read_bytes() == before


@pytest.mark.parametrize("native", ["true", "false", None])
def test_native_source_acquire_bypasses_input_and_legacy_export(tmp_path, monkeypatch, native):
    if native is None:
        monkeypatch.delenv("BAIBAI_EDINET_LIST_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", native)
    monkeypatch.setattr(
        "baibai_jobs.integration.source_handoff._acquire_edinet_lists", lambda *a: True
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert (
        handoff_main(
            [
                "acquire",
                "--source",
                "edinet-list",
                "--asof",
                str(DAY),
                "--directory",
                str(tmp_path / "work"),
                "--database",
                str(tmp_path / "absent.sqlite"),
            ]
        )
        == 0
    )
    assert output.read_text() == "stage_ready=false\nnative_l1=true\n"
    assert not (tmp_path / "absent.sqlite").exists()


def test_native_archive_input_drops_list_copies_but_keeps_source_scope(tmp_path, native_mode):
    from baibai_engine.data_collection.providers.edinet.facts_scope import (
        FactsScope,
        merge_scope,
        read_scope,
    )

    _, owner = owner_at(tmp_path, [receipt()])
    database = tmp_path / "market.sqlite"
    materialize_lists(owner.freeze(), database)
    with closing(open_connection(database)) as conn, conn:
        merge_scope(conn, FactsScope(DAY, frozenset({"S100TEST"}), frozenset()))
    output = tmp_path / "input.sqlite"
    export_source_input(database, output, group="edinet-facts", asof=DAY)
    with closing(open_connection(output)) as conn:
        assert read_scope(conn).doc_ids == frozenset({"S100TEST"})
        assert conn.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone() == (0,)


def test_legacy_archive_stage_can_only_replay_list_ids_already_migrated(tmp_path, monkeypatch):
    known = receipt()
    _, owner = owner_at(tmp_path, [known])
    database = tmp_path / "market.sqlite"
    materialize_lists(owner.freeze(), database)
    source = tmp_path / "stage.sqlite"
    with closing(open_connection(source)) as conn, conn:
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            (known.receipt_id, str(DAY), AT.isoformat(timespec="microseconds"), known.body_gzip),
        )
        conn.execute(
            "INSERT INTO edinet_source_archives VALUES('S100TEST',1,?,?)",
            (str(DAY), b"existing ZIP"),
        )
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    assert import_handoff(source, database).complete
    with closing(open_connection(source)) as conn, conn:
        unknown = receipt(at=AT + timedelta(seconds=1))
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            (
                unknown.receipt_id,
                str(DAY),
                unknown.received_at.isoformat(timespec="microseconds"),
                unknown.body_gzip,
            ),
        )
    before = database.read_bytes()
    with pytest.raises(ValueError, match="absent from fixed"):
        import_handoff(source, database)
    assert database.read_bytes() == before


def test_pending_publication_blocks_http_and_existing_stage_recovers_exact_receipt(
    tmp_path, monkeypatch
):
    from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY
    from baibai_jobs.acquisition.economic_staging import save_handoff
    from baibai_jobs.integration.edinet_list_native import export_pending_handoff
    from baibai_jobs.maintenance.edinet_list_native import commit_stage

    store, owner = owner_at(tmp_path)
    original_put = store.put_file

    def fail_head(key, path, **kwargs):
        if key == HEAD_KEY:
            raise OSError("interrupted head PUT")
        return original_put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", fail_head)
    report = acquire_lists(owner, Client(), start=DAY, end=DAY, today=DAY)
    assert report["failed"]
    pending = owner.pending_receipts()
    assert len(pending) == 1
    client = Client()
    with pytest.raises(LakePublishError, match=r"pending|resume"):
        acquire_lists(owner, client, start=DAY, end=DAY, today=DAY)
    assert client.calls == []
    assert export_pending_handoff(owner, asof=DAY, started_at=AT)
    save_handoff(store, "edinet-list-native-failed", owner.directory, market=True)
    monkeypatch.setattr(store, "put_file", original_put)
    cold = EdinetListL1(store, tmp_path / "cold")
    result = commit_stage(cold, store, "edinet-list-native-failed")
    assert result["receipt_count"] == 1
    assert tuple(cold.freeze().iter_receipts()) == tuple(pending)
    assert "coordination/market-sources/edinet-list-native-failed/integrated.json" in store.values
    # An acknowledgement retry neither GETs nor invents a new UUID/clock.
    commit_stage(EdinetListL1(store, tmp_path / "retry"), store, "edinet-list-native-failed")
    assert tuple(cold.freeze().iter_receipts()) == tuple(pending)


def test_unexpected_decode_failure_cannot_lose_received_body(tmp_path, monkeypatch):
    from baibai_jobs.acquisition import edinet_list_native as acquire

    _, owner = owner_at(tmp_path)
    monkeypatch.setattr(
        acquire, "project_document_list", lambda _: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError, match="stop"):
        acquire_lists(owner, Client(), start=DAY, end=DAY, today=DAY)
    saved = tuple(owner.freeze().iter_receipts())
    assert len(saved) == 1
    assert saved[0].decoded().body == payload(DAY)
    assert saved[0].received_at == AT + timedelta(seconds=1)


def test_bootstrap_store_requests_do_not_multiply_old_history(tmp_path):
    from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY

    originals = [receipt(DAY - timedelta(days=i), at=AT - timedelta(days=i)) for i in range(1, 101)]
    store, owner = owner_at(tmp_path, originals)
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()
    client = Client()
    report = acquire_lists(owner, client, start=DAY - timedelta(days=540), end=DAY, today=DAY)
    assert report["received_count"] == 541
    assert len(client.calls) == 541
    assert store.put_keys.count(HEAD_KEY) == 541
    # Each prior immutable body is checked once at entry; each new one once at publication.
    body_heads = [key for key in store.head_keys if key != HEAD_KEY]
    assert len(body_heads) <= 100 + 541 * 4
    assert len(store.get_keys) <= 3 * 541 + 110


def test_verified_cache_cleanup_keeps_source_and_rejects_unpublished_raw(tmp_path):
    from baibai_jobs.integration.edinet_list_native import clear_verified_receipt_cache

    store, owner = owner_at(tmp_path, [receipt()])
    database = tmp_path / "market.sqlite"
    fixed = owner.freeze()
    materialize_lists(fixed, database)
    before = {key: value.body for key, value in store.values.items()}
    assert clear_verified_receipt_cache(fixed, database) == 1
    assert contents(database)["edinet_list_responses"] == []
    materialize_lists(fixed, database)
    assert len(contents(database)["edinet_list_responses"]) == 1
    foreign = receipt(at=AT + timedelta(seconds=1))
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            (
                foreign.receipt_id,
                DAY.isoformat(),
                foreign.received_at.isoformat(timespec="microseconds"),
                foreign.body_gzip,
            ),
        )
    with pytest.raises(ValueError, match=r"unpublished|differ"):
        clear_verified_receipt_cache(fixed, database)
    assert len(contents(database)["edinet_list_responses"]) == 2
    assert {key: value.body for key, value in store.values.items()} == before


def test_native_cache_dehydrate_and_hydrate_roundtrip_without_lake_raw(tmp_path, monkeypatch):
    import hashlib

    from tests.helpers.lake_policy import narrow_release_policy

    from baibai_engine.storage.snapshots.duck import lake_session
    from baibai_engine.storage.snapshots.hydrate import (
        LakeHydrateError,
        dehydrate_market_store,
        hydrate_market_store,
    )
    from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
    from baibai_engine.storage.snapshots.reader import resolve_release
    from baibai_engine.storage.snapshots.release import create_release
    from baibai_engine.storage.snapshots.writer import (
        export_dataset_snapshot,
        sealed_sqlite_snapshot,
    )
    from baibai_engine.storage.sqlite.source_cache.lake_origin import (
        LakeStoreOrigin,
        write_lake_store_origin,
    )
    from baibai_jobs.integration.edinet_list_native import clear_verified_receipt_cache
    from baibai_jobs.publication.datasets import LAKE_DATASETS
    from baibai_jobs.publication.policy import release_policy_for_profile

    _, owner = owner_at(tmp_path, [receipt()])
    fixed = owner.freeze()
    database = tmp_path / "market.sqlite"
    materialize_lists(fixed, database)
    before = contents(database)
    mirror = tmp_path / "lake"
    narrow_release_policy(monkeypatch, datasets=("edinet.document_lists",))
    # A one-dataset synthetic release still has a required dataset.
    from baibai_jobs.publication import policy

    monkeypatch.setattr(
        policy,
        "PRODUCTION_RELEASE_POLICY",
        policy.PRODUCTION_RELEASE_POLICY.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(update={"required": True})
                    for item in policy.PRODUCTION_RELEASE_POLICY.datasets
                )
            }
        ),
    )
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
            dataset=LAKE_DATASETS["edinet.document_lists"],
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="lists",
            created_at=AT + timedelta(days=1),
        )
    path, manifest = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="no-raw",
        created_at=AT + timedelta(days=1),
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    with closing(open_connection(database)) as conn, conn:
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
    with pytest.raises(LakeHydrateError, match=r"edinet\.list_responses"):
        dehydrate_market_store(database, release=release, catalog=LAKE_DATASETS)
    clear_verified_receipt_cache(fixed, database)
    dehydrate_market_store(database, release=release, catalog=LAKE_DATASETS)
    assert contents(database)["edinet_document_lists"] == []
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(tmp_path / "cache", source),
            store=database,
            dataset_names=release.dataset_names(),
            catalog=LAKE_DATASETS,
        )
    assert contents(database)["edinet_list_responses"] == []
    materialize_lists(fixed, database)
    assert contents(database) == before


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_workflow_route_exports_pending_even_when_acquirer_returns_failure(
    tmp_path, monkeypatch, failure, native_mode
):
    from baibai_engine.data_collection.providers.edinet import http
    from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY
    from baibai_jobs.acquisition.economic_staging import save_handoff
    from baibai_jobs.integration import source_handoff
    from baibai_jobs.maintenance.edinet_list_native import commit_stage

    store, _ = owner_at(tmp_path)
    store.close = lambda: None
    client = Client()
    client.close = lambda: None
    monkeypatch.setattr(http, "EDINETHTTP", lambda _: client)
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **_: store)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    real_put = store.put_file

    def fail_head(key, path, **kwargs):
        if key == HEAD_KEY:
            raise failure("uncertain publication")
        return real_put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", fail_head)
    directory = tmp_path / "handoff"
    assert (
        handoff_main(
            [
                "acquire",
                "--source",
                "edinet-list",
                "--asof",
                DAY.isoformat(),
                "--directory",
                str(directory),
            ]
        )
        == 1
    )
    assert output.read_text().splitlines() == ["stage_ready=true"]
    report = json.loads((directory / "result.json").read_text())
    assert report["native_l1_pending"] is True
    assert len(client.calls) == 1
    save_handoff(store, "edinet-list-workflow-failed", directory, market=True)
    monkeypatch.setattr(store, "put_file", real_put)
    result = commit_stage(
        EdinetListL1(store, tmp_path / "recovery"), store, "edinet-list-workflow-failed"
    )
    assert result["receipt_count"] == 1
    assert len(client.calls) == 1


@pytest.mark.parametrize("command", ["hydrate", "dehydrate"])
def test_snapshot_cli_verifies_native_cache_before_generic_lake_write(
    tmp_path, monkeypatch, command, native_mode
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from baibai_engine.storage.objects import r2
    from baibai_jobs.publication import snapshot_write_cli as cli

    store, owner = owner_at(tmp_path, [receipt()])
    store.close = lambda: None
    database = tmp_path / "market.sqlite"
    materialize_lists(owner.freeze(), database)
    monkeypatch.setattr(r2, "Boto3R2Store", lambda **_: store)
    release = SimpleNamespace(dataset_names=lambda: ("edinet.document_lists",))
    cache = SimpleNamespace(source=None)

    @contextmanager
    def opened(**kwargs):
        yield None, cache

    @contextmanager
    def prefetched(*args, **kwargs):
        yield cache

    monkeypatch.setattr(cli, "open_lake", opened)
    monkeypatch.setattr(cli, "prefetching_hydration_cache", prefetched)
    monkeypatch.setattr(cli, "_resolve_release_target", lambda *args: (release, None))
    seen = []

    def write(*args, **kwargs):
        assert contents(database)["edinet_list_responses"] == []
        assert len(contents(database)["edinet_document_lists"]) == 1
        seen.append(command)
        return SimpleNamespace(as_dict=lambda: {"status": "ok"})

    monkeypatch.setattr(cli, command + "_market_store", write)
    assert (
        cli.main(
            [
                command,
                "--mirror",
                str(tmp_path / "mirror"),
                "--store",
                str(database),
                "--bucket",
                "synthetic",
            ]
        )
        == 0
    )
    assert seen == [command]
    assert len(tuple(owner.freeze().iter_receipts())) == 1


def test_native_daily_input_cli_skips_list_and_publishes_every_other_group(
    tmp_path, monkeypatch, capsys, native_mode
):
    from baibai_jobs.acquisition import source_inputs
    from baibai_jobs.integration import sources

    store = MemoryR2Store()
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(sources, "_load_handle", lambda _: object())
    monkeypatch.setattr(sources, "verify_held", lambda *_: None)
    monkeypatch.setattr(
        sources,
        "canonical_generations",
        lambda _: {"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64},
    )
    published = []
    articles = []
    monkeypatch.setattr(
        source_inputs, "_publish_one", lambda *args, **kwargs: published.append(kwargs["group"])
    )
    monkeypatch.setattr(
        sources, "publish_planning_input", lambda _, group, generations: articles.append(group)
    )
    argv = [
        "inputs",
        "--directory",
        str(tmp_path / "integration"),
        "--identity",
        "cycle",
        "--asof",
        DAY.isoformat(),
        "--lease-handle",
        str(tmp_path / "lease"),
    ]
    assert sources.main(argv) == 0
    expected = [group for group in sources.SOURCE_INPUT_GROUPS if not group.startswith("edinet-")]
    assert published == expected
    assert articles == list(sources.ARTICLE_SOURCES)
    report = json.loads(capsys.readouterr().out)
    assert report["updated"] == expected + list(sources.ARTICLE_SOURCES)
    assert report["failed"] == []
    published.clear()
    assert sources.main([*argv, "--group", "edinet-list"]) == 1
    assert published == []
    assert "common source integration failed: ValueError" in capsys.readouterr().err
