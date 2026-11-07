"""同じ固定L1から、旧metric状態に依存しない比較用候補を作る。"""

import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.observations.financials import edinet_integration
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import service
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    DEFERRED_CSV_REASON,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metric_baseline,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration import edinet_metric_rebuild
from baibai_jobs.integration.edinet_archive_native import rebuild_fixed_edinet_input
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.integration.edinet_metric_rebuild import rebuild_fixed_edinet_metrics

BASE = date(2026, 4, 24)
TARGET = date(2026, 4, 27)
AT = datetime(2026, 4, 27, 12, tzinfo=UTC)


def document(doc_id="S100ONE", ticker="72030", **fields):
    return {
        **_edinet_document(doc_id=doc_id, ticker=ticker),
        "docInfoEditStatus": "0",
        **fields,
    }


def receipt(day, documents, number, *, count=None, received_at=AT):
    body = json.dumps(
        {
            "metadata": {"resultset": {"count": len(documents) if count is None else count}},
            "results": [{**item, "seqNumber": i} for i, item in enumerate(documents, 1)],
        }
    ).encode()
    return NativeListReceipt.from_response(
        day, EdinetResponse(received_at, body), UUID(int=number).hex
    )


def sources(tmp_path, *, days=None, bodies=None):
    store = MemoryR2Store()
    list_owner = EdinetListL1(store, tmp_path / "lists")
    list_owner.freeze(require_existing=False)
    lists = list_owner.publish(
        [
            receipt(day, docs, n)
            for n, (day, docs) in enumerate(
                ({BASE: [document()]} if days is None else days).items(), 1
            )
        ]
    )
    archive_owner = EdinetArchiveL1(store, tmp_path / "archives")
    archives = archive_owner.initialize()
    if bodies is None:
        bodies = {("S100ONE", 5): _edinet_csv_zip()}
    if bodies:
        archives = archive_owner.publish(
            [
                NativeArchive.from_response(doc_id, kind, EdinetResponse(AT, body))
                for (doc_id, kind), body in bodies.items()
            ]
        )
    store.get_keys.clear()
    store.put_keys.clear()
    return store, list_owner, archive_owner, lists, archives


def run(lists, archives, output, *, asof=BASE, lookback_days=0):
    return rebuild_fixed_edinet_metrics(
        lists, archives, output, asof=asof, lookback_days=lookback_days
    )


def metrics(output, asof=TARGET):
    baseline = read_edinet_metric_baseline(output, asof)
    assert baseline is not None
    return baseline


@pytest.mark.parametrize("old_state", ["absent", "normal", "different_values", "corrupt"])
def test_fixed_candidate_ignores_old_metrics_and_matches_complete_source(
    tmp_path, monkeypatch, old_state
):
    store, _, _, lists, archives = sources(tmp_path)
    canonical = tmp_path / "market.sqlite"
    rebuild_fixed_edinet_input(lists, archives, canonical)
    edinet_integration.extract_saved_edinet(canonical, asof=BASE, lookback_days=0)
    expected = metrics(canonical).rows
    with sqlite3.connect(canonical) as connection:
        if old_state == "absent":
            connection.execute("DELETE FROM edinet_metrics")
            connection.execute("DELETE FROM source_coverage WHERE source='edinet_metrics'")
        elif old_state == "different_values":
            connection.execute("UPDATE edinet_metrics SET cash=77")
        elif old_state == "corrupt":
            connection.execute("UPDATE edinet_metrics SET failure_reasons='not JSON'")
            connection.execute(
                "UPDATE source_coverage SET record_count=999 WHERE source='edinet_metrics'"
            )
    original_market = canonical.read_bytes()
    original_source = dict(store.values)
    baseline_reader = read_edinet_metric_baseline

    def only_fresh_baseline(path, asof):
        assert path != canonical
        return baseline_reader(path, asof)

    def no_sql_archive(*args, **kwargs):
        raise AssertionError("metrics must read selected ZIPs from the fixed native owner")

    monkeypatch.setattr(edinet_integration, "read_edinet_metric_baseline", only_fresh_baseline)
    monkeypatch.setattr(service, "read_edinet_metric_baseline", only_fresh_baseline)
    monkeypatch.setattr(EDINETReader, "read_csv_zip", no_sql_archive)
    store.put_keys.clear()
    for n in range(2):
        output = tmp_path / f"candidate-{n}.sqlite"
        report = run(lists, archives, output)
        assert report["edinet-metrics"]["status"] == "completed"
        assert report["list_identity"] == lists.identity
        assert report["archive_identity"] == archives.identity
        assert report["output_written"]
        assert metrics(output).rows == expected
        assert metrics(output).rows["7203"].record.cash == 1000
        assert EDINETReader(output).load_metric_records(BASE)["7203"].cash == 1000
        with sqlite3.connect(output) as connection:
            assert (
                connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone()[0] == 0
            )
            assert (
                connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone()[0] == 0
            )
    assert canonical.read_bytes() == original_market
    assert store.values == original_source
    assert store.put_keys == []


def test_only_selected_csv_is_read_from_saved_heads(tmp_path):
    store, _, owner, lists, archives = sources(
        tmp_path,
        bodies={
            ("S100ONE", 5): _edinet_csv_zip(),
            ("S100ONE", 1): b"unrelated annual XBRL",
            ("S100OTHER", 5): b"unselected history",
        },
    )
    owner.publish([NativeArchive.from_response("S100LATER", 5, EdinetResponse(AT, b"later"))])
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    store.get_keys.clear()
    output = tmp_path / "candidate.sqlite"
    run(lists, archives, output)
    assert [key for key in store.get_keys if key.endswith(".zip")] == [
        f"l1/edinet/archives/v1/objects/{archives.metadata('S100ONE', 5).sha256}.zip"
    ]
    assert LIST_HEAD not in store.get_keys
    assert ARCHIVE_HEAD not in store.get_keys
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT received_at_utc FROM edinet_list_responses").fetchone()[
            0
        ] == AT.isoformat(timespec="microseconds")
    assert datetime.fromisoformat(metrics(output).recorded_at_utc) != AT


def test_partial_and_all_missing_are_decided_from_source_inventory(tmp_path):
    docs = [document(), document("S100OTHER", "83060")]
    _, _, owner, lists, empty = sources(tmp_path, days={BASE: docs}, bodies={})
    output = tmp_path / "absent.sqlite"
    report = run(lists, empty, output)
    assert report["edinet-metrics"]["status"] == "deferred_input"
    assert report["edinet-metrics"]["missing_archive_count"] == 2
    assert not report["output_written"]
    assert not output.exists()
    partial = owner.publish(
        [NativeArchive.from_response("S100OTHER", 5, EdinetResponse(AT, _edinet_csv_zip()))]
    )
    output = tmp_path / "partial.sqlite"
    report = run(lists, partial, output)
    assert report["edinet-metrics"]["status"] == "partial"
    assert metrics(output).rows["7203"].record.failure_reasons == (DEFERRED_CSV_REASON,)
    assert metrics(output).rows["8306"].record.cash == 1000
    assert set(EDINETReader(output).load_metric_records(BASE)) == {"8306"}
    complete = owner.publish(
        [NativeArchive.from_response("S100ONE", 5, EdinetResponse(AT, _edinet_csv_zip()))]
    )
    output = tmp_path / "complete.sqlite"
    assert run(lists, complete, output)["edinet-metrics"]["status"] == "completed"
    assert set(EDINETReader(output).load_metric_records(BASE)) == {"7203", "8306"}
    # Advancing current head cannot silently fill an older fixed missing input.
    assert (
        run(lists, empty, tmp_path / "still-absent.sqlite")["edinet-metrics"]["status"]
        == "deferred_input"
    )


@pytest.mark.parametrize("failure", ["invalid_zip", "missing_body", "changed_body"])
def test_broken_selected_original_does_not_publish_candidate(tmp_path, failure):
    store, _, _, lists, archives = sources(
        tmp_path,
        bodies={("S100ONE", 5): b"broken ZIP" if failure == "invalid_zip" else _edinet_csv_zip()},
    )
    key = next(key for key in store.values if key.endswith(".zip"))
    if failure == "missing_body":
        del store.values[key]
    elif failure == "changed_body":
        store.store_object(key, b"changed")
    output = tmp_path / "candidate.sqlite"
    if failure == "invalid_zip":
        report = run(lists, archives, output)
        assert report["edinet-metrics"]["status"] == "failed"
        assert report["edinet-metrics"]["exit_code"] == 1
        assert not report["output_written"]
    else:
        with pytest.raises(LakePublishError):
            run(lists, archives, output)
    assert not output.exists()
    assert not list(tmp_path.glob("edinet-metrics-*"))


def test_delayed_list_keeps_source_date_and_incomplete_window_defers(tmp_path):
    store, _, _, lists, archives = sources(tmp_path)
    output = tmp_path / "delayed.sqlite"
    report = run(lists, archives, output, asof=TARGET)
    assert report["requested_asof"] == TARGET.isoformat()
    assert report["edinet-metrics"]["input_asof"] == BASE.isoformat()
    assert report["edinet-metrics"]["snapshot_asof"] == BASE.isoformat()
    assert report["edinet-metrics"]["deferred_inputs"] == ["latest_document_list_delayed"]
    assert metrics(output).asof_date == BASE
    store.get_keys.clear()
    incomplete = tmp_path / "incomplete.sqlite"
    report = run(lists, archives, incomplete, lookback_days=1)
    assert report["edinet-metrics"]["deferred_inputs"] == ["document_window_incomplete"]
    assert not incomplete.exists()
    assert not any(key.endswith(".zip") for key in store.get_keys)


@pytest.mark.parametrize(
    ("days", "reason"), [({}, "document_list_unavailable"), ({BASE: []}, "no_selected_filings")]
)
def test_no_list_and_normal_empty_remain_distinct_deferred_inputs(tmp_path, days, reason):
    store, _, _, lists, archives = sources(tmp_path, days=days)
    output = tmp_path / "candidate.sqlite"
    report = run(lists, archives, output)
    assert report["edinet-metrics"]["deferred_inputs"] == [reason]
    assert not output.exists()
    assert not any(key.endswith(".zip") for key in store.get_keys)


@pytest.mark.parametrize("failure", ["missing_list_body", "negative_lookback"])
def test_unusable_input_never_becomes_a_candidate(tmp_path, failure):
    store, _, _, lists, archives = sources(tmp_path)
    output = tmp_path / "candidate.sqlite"
    if failure == "missing_list_body":
        del store.values[next(key for key in store.values if key.endswith(".body.gz"))]
        with pytest.raises(LakePublishError):
            run(lists, archives, output)
    else:
        with pytest.raises(ValueError, match="lookback"):
            run(lists, archives, output, lookback_days=-1)
        assert store.get_keys == []
    assert not output.exists()


@pytest.mark.parametrize("event", ["correction", "edit", "parent_withdrawal"])
def test_source_operations_select_the_same_filings_before_direct_zip_reads(tmp_path, event):
    old = document()
    changed = document("S100NEXT", parentDocID="S100ONE", submitDateTime="2026-04-27 12:00")
    initial = [old, document("S100OTHER", "83060")]
    if event == "correction":
        changed["docTypeCode"] = "130"
    elif event == "edit":
        changed = {
            **old,
            "docInfoEditStatus": "1",
            "docDescription": "edited",
            "opeDateTime": "2026-04-27 12:00",
        }
    else:
        initial.append(document("S100CHILD", docTypeCode="130", parentDocID="S100ONE"))
        changed["withdrawalStatus"] = "1"
    days = {
        BASE: initial,
        BASE + timedelta(days=1): [],
        BASE + timedelta(days=2): [],
        TARGET: [changed],
    }
    store, _, _, lists, archives = sources(
        tmp_path,
        days=days,
        bodies={
            (key, 5): _edinet_csv_zip() for key in ("S100ONE", "S100OTHER", "S100NEXT", "S100CHILD")
        },
    )
    output = tmp_path / "candidate.sqlite"
    report = run(lists, archives, output, asof=TARGET, lookback_days=3)
    assert report["edinet-metrics"]["status"] == "completed"
    rows = metrics(output).rows
    if event == "parent_withdrawal":
        assert set(rows) == {"8306"}
    else:
        assert rows["7203"].record.source_doc_id == (
            "S100NEXT" if event == "correction" else "S100ONE"
        )
    assert rows["8306"].record.cash == 1000
    assert len([key for key in store.get_keys if key.endswith(".zip")]) == len(rows)


@pytest.mark.parametrize("origin", ["unknown", "known_ticker", "retained_outside_window"])
def test_operation_identity_comes_only_from_retained_lists(tmp_path, origin):
    event = {
        "docID": "S100WITHDRAW",
        "parentDocID": "S100OLD",
        "withdrawalStatus": "1",
        "disclosureStatus": "0",
        "legalStatus": "0",
        "docInfoEditStatus": "0",
    }
    if origin == "known_ticker":
        event.update(secCode="72030", docTypeCode="120")
    days = {BASE: [event, document(), document("S100OTHER", "83060")]}
    if origin == "retained_outside_window":
        days[date(2024, 4, 1)] = [
            document(
                "S100OLD",
                periodStart="2023-04-01",
                periodEnd="2024-03-31",
                submitDateTime="2024-04-01 12:00",
            )
        ]
    store, _, _, lists, archives = sources(
        tmp_path,
        days=days,
        bodies={(key, 5): _edinet_csv_zip() for key in ("S100ONE", "S100OTHER")},
    )
    output = tmp_path / "candidate.sqlite"
    report = run(lists, archives, output)
    assert report["edinet-metrics"]["status"] == "completed"
    record = metrics(output).rows["7203"].record
    if origin == "known_ticker":
        assert record.cash is None
        assert record.failure_reasons == ("document_event_quarantined:withdrawal:S100WITHDRAW",)
        assert len([key for key in store.get_keys if key.endswith(".zip")]) == 1
    else:
        assert record.cash == 1000
    if origin == "retained_outside_window":
        assert "quarantined_events" not in report["edinet-metrics"]
    else:
        assert report["edinet-metrics"]["quarantined_events"] == [
            {
                "doc_id": "S100WITHDRAW",
                "target_doc_id": "S100OLD",
                "event_type": "withdrawal",
                "document_type": "120" if origin == "known_ticker" else None,
                "ticker": "7203" if origin == "known_ticker" else None,
            }
        ]


def test_invalid_later_list_remains_failed_and_keeps_valid_prior(tmp_path):
    _, owner, _, _, archives = sources(tmp_path)
    lists = owner.publish(
        [
            NativeListReceipt.from_response(
                BASE, EdinetResponse(AT + timedelta(seconds=1), b"{"), UUID(int=20).hex
            )
        ]
    )
    output = tmp_path / "candidate.sqlite"
    report = run(lists, archives, output)
    assert report["edinet-list"]["status"] == "failed"
    assert report["edinet-list"]["exit_code"] == 1
    assert report["edinet-list"]["failed_days"] == [BASE.isoformat()]
    assert report["edinet-metrics"]["status"] == "completed"
    assert metrics(output).rows["7203"].record.cash == 1000


@pytest.mark.parametrize("event", ["correction", "withdrawal"])
def test_candidate_keeps_later_known_event_for_eligibility(tmp_path, event):
    docs = [document(), document("S100OTHER", "83060")]
    _, owner, _, _, archives = sources(
        tmp_path,
        days={BASE: docs},
        bodies={(key, 5): _edinet_csv_zip() for key in ("S100ONE", "S100OTHER")},
    )
    changed = document("S100EVENT", parentDocID="S100ONE", submitDateTime="2026-04-27 12:00")
    if event == "correction":
        changed["docTypeCode"] = "130"
    else:
        changed["withdrawalStatus"] = "1"
    # The calculation cutoff must not remove retained evidence used by later reads.
    lists = owner.publish([receipt(TARGET, [changed], 20)])
    output = tmp_path / "candidate.sqlite"
    report = run(lists, archives, output, asof=BASE)
    assert report["edinet-list"]["status"] == "completed"
    assert report["edinet-metrics"]["snapshot_asof"] == BASE.isoformat()
    assert set(metrics(output).rows) == {"7203", "8306"}
    assert set(EDINETReader(output).load_metric_records(BASE)) == {"7203", "8306"}
    assert set(EDINETReader(output).load_metric_records(TARGET)) == {"8306"}


@pytest.mark.parametrize("existing", ["file", "symlink", "race"])
def test_candidate_never_replaces_existing_output(tmp_path, monkeypatch, existing):
    store, _, _, lists, archives = sources(tmp_path)
    output = tmp_path / "existing.sqlite"
    if existing == "file":
        output.write_bytes(b"existing market")
    elif existing == "symlink":
        output.symlink_to(tmp_path / "missing-target")
    else:
        link = os.link

        def competing_output(source, destination):
            output.write_bytes(b"existing market")
            link(source, destination)

        monkeypatch.setattr(edinet_metric_rebuild.os, "link", competing_output)
    with pytest.raises(FileExistsError):
        run(lists, archives, output)
    if existing == "symlink":
        assert output.is_symlink()
    else:
        assert output.read_bytes() == b"existing market"
    if existing != "race":
        assert store.get_keys == []
    assert not list(tmp_path.glob("edinet-metrics-*"))


def test_retained_pair_reuses_cold_metric_reader_without_sql_archives(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.generation.edinet_inputs import (
        read_edinet_input,
        retain_edinet_input,
    )

    store, _, _, lists, archives = sources(tmp_path)
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    fixed = read_edinet_input(store, saved)
    before = dict(store.values)
    monkeypatch.setattr(EDINETReader, "read_csv_zip", lambda *a: pytest.fail("SQL ZIP read"))
    output = tmp_path / "candidate.sqlite"
    report = run(fixed.lists, fixed.archives, output)
    assert report["edinet-metrics"]["status"] == "completed"
    assert metrics(output).rows["7203"].record.cash == 1000
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone() == (0,)
    assert store.values == before


@pytest.mark.parametrize("current_filings", ["replacement_and_healthy", "healthy", "none"])
def test_omitted_origin_receipt_quarantines_anonymous_withdrawal_on_cold_and_warm_stores(
    tmp_path, current_filings
):
    store, owner, _, initial, archives = sources(
        tmp_path,
        bodies={
            ("S100ONE", 5): _edinet_csv_zip(),
            ("S100OTHER", 5): _edinet_csv_zip(),
        },
    )
    warm = tmp_path / "warm.sqlite"
    materialize_lists(initial, warm)
    edinet_integration.extract_saved_edinet(
        warm, asof=BASE, lookback_days=0, metric_archives=archives, fact_archives=archives
    )
    assert metrics(warm).rows["7203"].record.cash == 1000
    event = {
        "docID": "S100WITHDRAW",
        "parentDocID": "S100ONE",
        "withdrawalStatus": "1",
        "legalStatus": "0",
        "disclosureStatus": "0",
        "docInfoEditStatus": "0",
    }
    documents = [event]
    if current_filings != "none":
        documents.append(document("S100OTHER", "83060"))
    if current_filings == "replacement_and_healthy":
        # No CSV for the newly selected same-ticker filing: quarantine must
        # precede missing-archive planning, not parse it or claim a deferral.
        documents.append(document("S100NEW", "72030"))
    fixed = owner.publish(
        [
            receipt(BASE, [], 20, received_at=AT + timedelta(seconds=1)),
            receipt(TARGET, documents, 21, received_at=AT + timedelta(seconds=2)),
        ]
    )
    materialize_lists(fixed, warm)
    warm_report = edinet_integration.extract_saved_edinet(
        warm, asof=TARGET, lookback_days=0, metric_archives=archives, fact_archives=archives
    )
    expected = metrics(warm).rows
    if current_filings == "none":
        assert warm_report["edinet-metrics"]["status"] == "deferred_input"
        assert warm_report["edinet-metrics"]["deferred_inputs"] == ["no_selected_filings"]
        assert expected["7203"].record.cash == 1000
        assert metrics(warm).asof_date == BASE
    else:
        assert warm_report["edinet-metrics"]["status"] == "completed"
    original_warm = warm.read_bytes()
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    original_source = dict(store.values)
    store.get_keys.clear()
    output = tmp_path / "cold.sqlite"
    report = run(fixed, archives, output, asof=TARGET)
    assert report["output_written"]
    assert report["edinet-metrics"]["status"] == "completed"
    assert report["edinet-metrics"]["deferred_inputs"] == []
    rows = metrics(output).rows
    affected = rows["7203"].record
    assert affected.source_doc_id == "S100ONE"
    assert affected.cash is None
    assert affected.failure_reasons == ("document_event_quarantined:withdrawal:S100WITHDRAW",)
    if current_filings != "none":
        assert affected.failure_reasons == expected["7203"].record.failure_reasons
    if current_filings == "replacement_and_healthy":
        assert rows == expected
    else:
        # Raw identity alone cannot recreate the lost prior extraction's metadata.
        assert affected.document_type is None
        assert affected.source_submit_datetime is None
        assert affected.source_period_start is None
        assert affected.source_period_end is None
    wanted = {"7203"} if current_filings == "none" else {"7203", "8306"}
    assert set(rows) == wanted
    healthy = archives.metadata("S100OTHER", 5)
    assert [key for key in store.get_keys if key.endswith(".zip")] == (
        [] if current_filings == "none" else [f"l1/edinet/archives/v1/objects/{healthy.sha256}.zip"]
    )
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM edinet_documents WHERE doc_id='S100ONE'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM edinet_list_responses").fetchone() == (3,)
    assert warm.read_bytes() == original_warm
    assert store.values == original_source


def test_manual_extraction_uses_the_same_original_identity_without_metric_baseline(tmp_path):
    from tests.helpers.edinet_metrics import FakeEDINETMetricReader

    _, owner, _, _, _ = sources(tmp_path)
    event = {
        "docID": "S100WITHDRAW",
        "parentDocID": "S100ONE",
        "withdrawalStatus": "1",
        "legalStatus": "0",
        "disclosureStatus": "0",
        "docInfoEditStatus": "0",
    }
    fixed = owner.publish(
        [
            receipt(BASE, [], 20, received_at=AT + timedelta(seconds=1)),
            receipt(
                TARGET,
                [event, document("S100OTHER", "83060")],
                21,
                received_at=AT + timedelta(seconds=2),
            ),
        ]
    )
    database = tmp_path / "manual.sqlite"
    materialize_lists(fixed, database)
    assert read_edinet_metric_baseline(database, TARGET) is None
    reader = FakeEDINETMetricReader(
        documents=[event, document("S100OTHER", "83060")],
        zip_by_doc_id={"S100OTHER": _edinet_csv_zip()},
    )
    assert (
        service.extract_edinet_metrics_command(
            asof_date=TARGET, lookback_days=0, reader=reader, sqlite_path=database
        )
        == 0
    )
    assert reader.read_calls == ["S100OTHER"]
    assert metrics(database).rows["7203"].record.failure_reasons == (
        "document_event_quarantined:withdrawal:S100WITHDRAW",
    )


def test_read_eligibility_checks_retained_identity_before_the_next_extraction(tmp_path):
    _, owner, _, _, archives = sources(
        tmp_path,
        bodies={
            ("S100NEW", 5): _edinet_csv_zip(),
            ("S100OTHER", 5): _edinet_csv_zip(),
        },
    )
    fixed = owner.publish(
        [
            receipt(
                BASE,
                [document("S100NEW", "72030"), document("S100OTHER", "83060")],
                20,
                received_at=AT + timedelta(seconds=1),
            ),
        ]
    )
    database = tmp_path / "market.sqlite"
    materialize_lists(fixed, database)
    report = edinet_integration.extract_saved_edinet(
        database, asof=BASE, lookback_days=0, metric_archives=archives, fact_archives=archives
    )
    assert report["edinet-metrics"]["status"] == "completed"
    assert metrics(database).rows["7203"].record.source_doc_id == "S100NEW"
    assert set(EDINETReader(database).load_metric_records(BASE)) == {"7203", "8306"}
    fixed = owner.publish(
        [
            receipt(
                TARGET,
                [{"docID": "S100WITHDRAW", "parentDocID": "S100ONE", "withdrawalStatus": "1"}],
                21,
                received_at=AT + timedelta(seconds=2),
            ),
        ]
    )
    materialize_lists(fixed, database)
    before = database.read_bytes()
    assert set(EDINETReader(database).load_metric_records(TARGET)) == {"8306"}
    assert database.read_bytes() == before
    # Eligibility is read-only. The prior successful snapshot is still retained.
    assert metrics(database).rows["7203"].record.cash == 1000
