"""Native acquisition and one fixed archive head reach every existing SQL consumer."""

import json
from contextlib import closing
from datetime import timedelta
from unittest.mock import Mock

import pytest
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import (
    AT,
    DAY,
    filing,
    setup,
)
from tests.jobs.integration.test_edinet_fixed_consumer_input import (
    ASOF,
    KNOWN,
    consumer_outputs,
    fixtures,
)

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import edinet_archive_native as acquisition
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.integration import source_handoff, sources
from baibai_jobs.integration.edinet_archive_native import (
    export_pending_handoff,
    materialize_archives,
    rebuild_fixed_edinet_input,
)
from baibai_jobs.maintenance.edinet_archive_native import commit_stage
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


def native_cli(tmp_path, monkeypatch, *, group="edinet-facts", documents=None):
    owner, client, _, lists = setup(
        tmp_path, documents or [filing("S100ONE"), filing("S100TWO", "67580")]
    )
    owner.publish((), scope=FactsScope(since=DAY - timedelta(days=540)))
    owner.store.close = Mock()
    client.close = Mock()
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **_: owner.store)
    monkeypatch.setattr(
        "baibai_engine.data_collection.providers.edinet.http.EDINETHTTP", lambda _: client
    )
    # Keep the route's production default explicit. Tiny fixtures need only today's list.
    real_acquire = acquisition.acquire_archives

    def acquire(*args, **kwargs):
        assert "lookback_days" not in kwargs
        return real_acquire(*args, **kwargs, lookback_days=0)

    monkeypatch.setattr(acquisition, "acquire_archives", acquire)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    directory = tmp_path / "workflow"
    argv = [
        "acquire",
        "--source",
        group,
        "--asof",
        str(DAY),
        "--directory",
        str(directory),
        "--database",
        str(tmp_path / "absent-market.sqlite"),
    ]
    return owner, client, directory, output, argv, lists


@pytest.mark.parametrize("group", ["edinet-metrics", "edinet-facts"])
def test_workflow_acquires_without_input_market_l2_or_retained_body_get(
    tmp_path, monkeypatch, group
):
    owner, client, directory, output, argv, _ = native_cli(tmp_path, monkeypatch, group=group)
    assert source_handoff.main(argv) == 0
    assert client.fetch_archive.call_count == 2
    assert output.read_text().splitlines() == ["stage_ready=false", "native_l1=true"]
    assert not (directory / "receipts.sqlite").exists()
    assert not (tmp_path / "absent-market.sqlite").exists()
    before = owner.freeze().value()
    body_keys = {key for key in owner.store.values if key.endswith(".zip")}
    owner.store.get_keys.clear()
    client.fetch_archive.reset_mock()
    assert source_handoff.main(argv) == 0
    client.fetch_archive.assert_not_called()
    assert not body_keys.intersection(owner.store.get_keys)
    assert not any(key.startswith("coordination/") for key in owner.store.get_keys)
    assert owner.freeze().value() == before


def test_later_rate_limit_keeps_prior_native_original_and_failure_status(tmp_path, monkeypatch):
    owner, client, directory, output, argv, _ = native_cli(tmp_path, monkeypatch)
    response = client.fetch_archive.return_value
    client.fetch_archive.side_effect = [response, EdinetRateLimitError("limited")]
    assert source_handoff.main(argv) == 1
    assert len(owner.freeze().archive_keys) == 1
    assert owner.freeze().scope.doc_ids == frozenset({"S100ONE", "S100TWO"})
    assert output.read_text().splitlines() == ["stage_ready=false", "native_l1=true"]
    assert not (directory / "receipts.sqlite").exists()


@pytest.mark.parametrize(
    ("boundary", "failure"),
    [
        ("put", LakeCASConflict),
        ("put", LakePublishError),
        ("put", OSError),
        ("read_after_http", LakePublishError),
        ("read_after_http", OSError),
    ],
)
def test_failed_head_publication_stages_exact_work_and_recovers_without_http(
    tmp_path, monkeypatch, boundary, failure
):
    owner, client, directory, output, argv, _ = native_cli(tmp_path, monkeypatch)
    real_put = owner.store.put_file
    real_get = owner.store.get_bytes

    def fail_head(key, path, **kwargs):
        if key == ARCHIVE_HEAD:
            raise failure("synthetic private failure")
        return real_put(key, path, **kwargs)

    def fail_read(key):
        if key == ARCHIVE_HEAD:
            raise failure("synthetic private failure")
        return real_get(key)

    def receive(*_):
        monkeypatch.setattr(owner.store, "get_bytes", fail_read)
        return client.fetch_archive.return_value

    # Fail after selection scope was already committed, when the first ZIP is retained.
    native = acquisition.acquire_archives

    def run(*args, **kwargs):
        args[0].publish((), scope=FactsScope(doc_ids=frozenset({"S100ONE", "S100TWO"})))
        if boundary == "put":
            monkeypatch.setattr(owner.store, "put_file", fail_head)
        else:
            client.fetch_archive.side_effect = receive
        return native(*args, **kwargs)

    monkeypatch.setattr(acquisition, "acquire_archives", run)
    assert source_handoff.main(argv) == 1
    assert client.fetch_archive.call_count == 1
    assert output.read_text().splitlines() == ["stage_ready=true"]
    report = json.loads((directory / "result.json").read_bytes())
    assert report["native_l1_pending"] is True
    assert report["native_archive_head"]["archives"][0][
        "received_at_utc"
    ] == AT.isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(owner.store, "put_file", real_put)
    monkeypatch.setattr(owner.store, "get_bytes", real_get)
    save_handoff(owner.store, "edinet-facts-native-failure", directory, market=True)
    recovered = EdinetArchiveL1(owner.store, tmp_path / "recovered")
    commit_stage(recovered, owner.store, "edinet-facts-native-failure")
    assert len(recovered.freeze().archive_keys) == 1
    assert recovered.freeze().metadata("S100ONE", 1).received_at_utc == AT
    assert recovered.freeze().read_xbrl_zip("S100ONE") == client.fetch_archive.return_value.body
    assert available_handoffs(owner.store, source="edinet-facts") == []
    assert client.fetch_archive.call_count == 1


def test_common_plan_fixes_both_heads_and_updates_all_consumers_without_latest(
    tmp_path, monkeypatch
):
    store, _, owner, lists, archives, _ = fixtures(tmp_path)
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    work = tmp_path / "integration"
    assert sources.main(["plan", "--directory", str(work)]) == 0
    fixed_plan = json.loads((work / "plan.json").read_bytes())
    assert fixed_plan["edinet_list_head"] == lists.value()
    assert fixed_plan["edinet_archive_head"] == archives.value()
    owner.publish([NativeArchive.from_response("S100LATER", 1, EdinetResponse(KNOWN, b"later"))])
    del store.values[ARCHIVE_HEAD]
    del store.values[LIST_HEAD]
    store.get_keys.clear()
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    applied = sources.apply(store, work, market=market, asof=ASOF)
    assert applied["extractions"]["native_archives"]["l1_identity"] == archives.identity
    assert ARCHIVE_HEAD not in store.get_keys
    assert LIST_HEAD not in store.get_keys
    cold = tmp_path / "cold.sqlite"
    rebuild_fixed_edinet_input(lists, archives, cold)
    assert consumer_outputs(market) == consumer_outputs(cold)
    with closing(open_connection(market)) as connection:
        assert read_scope(connection) == archives.scope
        assert connection.execute(
            "SELECT count(*) FROM edinet_source_archives WHERE doc_id='S100LATER'"
        ).fetchone() == (0,)


def test_same_head_reuses_all_bytes_and_reconciles_only_execution_copy_receipt_day(tmp_path):
    store, _, _, _, archives, _ = fixtures(tmp_path)
    database = tmp_path / "market.sqlite"
    materialize_archives(archives, database)
    before_remote = dict(store.values)
    with closing(open_connection(database)) as connection, connection:
        connection.execute("UPDATE edinet_source_archives SET stored_on='2026-11-04'")
    store.get_keys.clear()
    result = materialize_archives(archives, database)
    assert result["copied_archives"] == 0
    assert store.get_keys == []
    with closing(open_connection(database)) as connection:
        assert connection.execute(
            "SELECT DISTINCT stored_on FROM edinet_source_archives"
        ).fetchall() == [("2026-11-02",)]
    assert store.values == before_remote


@pytest.mark.parametrize("broken", ["bytes", "unknown", "earlier_day", "scope"])
def test_unmigrated_or_changed_local_original_stops_without_modifying_it(tmp_path, broken):
    store, _, _, _, archives, _ = fixtures(tmp_path)
    database = tmp_path / "market.sqlite"
    materialize_archives(archives, database)
    with closing(open_connection(database)) as connection, connection:
        if broken == "bytes":
            connection.execute("UPDATE edinet_source_archives SET zip_content=x'aa'")
        elif broken == "unknown":
            connection.execute(
                "INSERT INTO edinet_source_archives VALUES('S100OTHER',1,'2026-11-02',x'aa')"
            )
        elif broken == "earlier_day":
            connection.execute("UPDATE edinet_source_archives SET stored_on='2026-11-01'")
        else:
            merge_scope(connection, FactsScope(doc_ids=frozenset({"S100OTHER"})))
    before = database.read_bytes()
    store.get_keys.clear()
    with pytest.raises(ValueError, match="fixed native head"):
        materialize_archives(archives, database)
    assert database.read_bytes() == before
    assert store.get_keys == []


def test_missing_fixed_object_rolls_back_partial_sql_and_never_resolves_current(tmp_path):
    store, _, _, _, archives, _ = fixtures(tmp_path)
    database = tmp_path / "market.sqlite"
    with closing(open_connection(database)):
        pass
    before = database.read_bytes()
    metadata = archives.metadata(*sorted(archives.archive_keys)[-1])
    del store.values[f"l1/edinet/archives/v1/objects/{metadata.sha256}.zip"]
    store.get_keys.clear()
    with pytest.raises(LakePublishError, match="missing or changed"):
        materialize_archives(archives, database)
    assert database.read_bytes() == before
    assert ARCHIVE_HEAD not in store.get_keys


@pytest.mark.parametrize("command", ["input", "resume"])
def test_retired_source_input_route_stops_before_remote_or_sql(tmp_path, monkeypatch, command):
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    factory = Mock(side_effect=AssertionError("must not contact canonical storage"))
    monkeypatch.setattr(source_handoff, "Boto3R2Store", factory)
    assert (
        source_handoff.main(
            [
                command,
                "--source",
                "edinet-facts",
                "--directory",
                str(tmp_path / "input"),
                "--asof",
                str(DAY),
                "--database",
                str(tmp_path / "absent.sqlite"),
            ]
        )
        == 1
    )
    factory.assert_not_called()
    assert not (tmp_path / "absent.sqlite").exists()


def test_explicit_zip_refresh_requires_native_head_before_http_or_sql(tmp_path, monkeypatch):
    owner, _, _, _, _, _ = native_cli(tmp_path, monkeypatch)
    del owner.store.values[ARCHIVE_HEAD]
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_refresh.edinet.Boto3R2Store", lambda **_: owner.store
    )
    path = tmp_path / "absent.sqlite"
    with (
        closing(EDINETRefresh("unused", sqlite_path=path)) as client,
        pytest.raises(LakePublishError, match="head is missing"),
    ):
        client.download_xbrl_zip("S100ONE")
    assert not path.exists()


def test_missing_native_head_cannot_initialize_from_old_input_or_start_http(tmp_path, monkeypatch):
    owner, client, _, output, argv, _ = native_cli(tmp_path, monkeypatch)
    del owner.store.values[ARCHIVE_HEAD]
    assert source_handoff.main(argv) == 1
    client.fetch_archive.assert_not_called()
    assert ARCHIVE_HEAD not in owner.store.values
    assert not output.exists()


def test_scope_only_pending_work_keeps_unavailable_requests_and_remains_explicit(
    tmp_path, monkeypatch
):
    owner, _, directory, _, _, _ = native_cli(tmp_path, monkeypatch)
    target = EdinetArchiveL1(owner.store, directory)
    put = owner.store.put_file
    monkeypatch.setattr(owner.store, "put_file", Mock(side_effect=LakeCASConflict("lost CAS")))
    requested = FactsScope(doc_ids=frozenset({"S100MISSING"}))
    with pytest.raises(LakeCASConflict):
        target.publish((), scope=requested)
    assert export_pending_handoff(target, source="edinet-facts", asof=DAY, started_at=AT)
    monkeypatch.setattr(owner.store, "put_file", put)
    save_handoff(owner.store, "edinet-facts-scope-failure", directory, market=True)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: owner.store)
    work = tmp_path / "integration"
    assert sources.main(["plan", "--directory", str(work)]) == 0
    assert json.loads((work / "plan.json").read_bytes())["handoffs"] == []
    assert len(available_handoffs(owner.store, source="edinet-facts")) == 1
    recovered = EdinetArchiveL1(owner.store, tmp_path / "recovery")
    commit_stage(recovered, owner.store, "edinet-facts-scope-failure")
    assert "S100MISSING" in recovered.freeze().scope.doc_ids
    assert not recovered.freeze().archive_keys
    assert available_handoffs(owner.store, source="edinet-facts") == []


def test_legacy_list_only_native_plan_is_rejected_before_any_cache_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"edinet_list_native": True, "handoffs": []}))
    database = tmp_path / "absent.sqlite"
    with pytest.raises(ValueError, match="archive head is absent"):
        sources.apply(Mock(), work, market=database, asof=ASOF)
    assert not database.exists()


@pytest.mark.parametrize("group", ["edinet-list", "edinet-metrics", "edinet-facts"])
def test_legacy_stage_replay_and_export_cannot_write_outside_native_head(
    tmp_path, monkeypatch, group
):
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    target = tmp_path / "absent.sqlite"
    with pytest.raises(ValueError, match="cannot be replayed into a native execution copy"):
        source_handoff._replay(group, target, tmp_path / "absent-stage")
    with pytest.raises(ValueError, match="does not export legacy"):
        source_handoff._export(group, DAY, AT, target, tmp_path / "absent-stage")
    assert not target.exists()
    assert not (tmp_path / "absent-stage").exists()
