"""明示した保存pairのTOB読取は、SQL/current headを原本の代わりにしない。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.jobs.integration.test_edinet_fixed_consumer_input import (
    ARCHIVE_HEAD,
    ASOF,
    DAY,
    KNOWN,
    LIST_HEAD,
    csv_zip,
    fixtures,
    zipped,
)

from baibai_engine.data_collection.providers.edinet.archive_native import (
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.list_native import (
    FixedEdinetLists,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input
from baibai_engine.machine_analysis.observations.disclosures.delistings import (
    DelistingRecord,
    store_jpx_delistings,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    build_tender_offer_exit_values,
    read_tender_offer_exit_values,
    store_tender_offer_exit_values,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_jobs.integration.edinet_archive_native import materialize_archives
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.maintenance.market import cli, delisting_cli

RESULT = "S100RESULT"
OFFER = "S100OFFER"


def _save(case):
    case.saved = retain_edinet_input(
        case.store, case.lists, case.archives, selected_at=case.selected_at
    )
    case.input_file.write_text(case.saved.model_dump_json())


def _case(tmp_path, monkeypatch):
    store, list_owner, archive_owner, lists, archives, _ = fixtures(tmp_path)
    rows = [
        DelistingRecord(
            delisted_on=ASOF,
            ticker="2000",
            name="fixture",
            market="プライム",
            reason="公開買付け",
        )
    ]
    legacy = tmp_path / "legacy.sqlite"
    materialize_lists(lists, legacy)
    materialize_archives(archives, legacy)
    store_jpx_delistings(legacy, rows)
    expected = build_tender_offer_exit_values(legacy, reader=EDINETReader(legacy), asof=ASOF)
    assert len(expected[0]) == 1
    market = tmp_path / "market.sqlite"
    store_jpx_delistings(market, rows)
    case = SimpleNamespace(
        store=store,
        list_owner=list_owner,
        archive_owner=archive_owner,
        lists=lists,
        archives=archives,
        expected=expected,
        market=market,
        selected_at=KNOWN + timedelta(days=1),
        input_file=tmp_path / "input.json",
    )
    _save(case)
    store.close = Mock()
    monkeypatch.setattr(delisting_cli, "Boto3R2Store", Mock(return_value=store))
    monkeypatch.setattr(cli, "load_project_env", Mock())
    monkeypatch.setattr(cli, "EDINETRefresh", Mock(side_effect=AssertionError("acquisition")))
    monkeypatch.setattr("requests.Session", Mock(side_effect=AssertionError("HTTP")))
    monkeypatch.setattr(EDINETReader, "read_csv_zip", Mock(side_effect=AssertionError("SQL ZIP")))
    return case


def _argv(case):
    return [
        "build-tender-offer-exits",
        "--sqlite-path",
        str(case.market),
        "--asof",
        ASOF.isoformat(),
        "--saved-input",
        str(case.input_file),
    ]


def _clear_trace(case):
    case.store.get_keys.clear()
    case.store.head_keys.clear()
    case.store.put_keys.clear()


def _body_key(case, doc_id=RESULT):
    metadata = case.archives.metadata(doc_id, 5)
    return "l1/edinet/archives/v1/objects/" + metadata.sha256 + ".zip"


def _replace_result(case, *, body=None, source_type=5, remove=False, coarse=False):
    value = case.archives.value()
    value["archives"] = [entry for entry in value["archives"] if entry["doc_id"] != RESULT]
    if not remove:
        content = case.archives.read_csv_zip(RESULT) if body is None else body
        item = NativeArchive.from_response(
            RESULT,
            source_type,
            EdinetResponse(datetime(2026, 11, 2, 15, 30, tzinfo=UTC), content),
        )
        metadata = (
            item.metadata.model_copy(update={"received_at_utc": None}) if coarse else item.metadata
        )
        value["archives"].append(metadata.model_dump(mode="json"))
        case.store.store_object(
            "l1/edinet/archives/v1/objects/" + metadata.sha256 + ".zip", content
        )
    case.archives = FixedEdinetArchives(case.store, value)
    _save(case)


@pytest.mark.parametrize("coarse", [False, True])
def test_saved_build_matches_sql_baseline_with_empty_raw_and_fixed_utc_jst_receipts(
    tmp_path, monkeypatch, capsys, coarse
):
    case = _case(tmp_path, monkeypatch)
    _replace_result(case, coarse=coarse)
    del case.store.values[LIST_HEAD]
    del case.store.values[ARCHIVE_HEAD]
    _clear_trace(case)
    before = dict(case.store.values)
    reads = []
    queries = []
    connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        path = connection.execute("PRAGMA database_list").fetchone()[2]
        initialized = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchone()
        connection.set_trace_callback(queries.append)
        if initialized is None:
            # Initial DDL declares archive indexes; it is not an inventory/body read.
            return connection

        def authorize(action, table, column, *unused):
            if action == sqlite3.SQLITE_READ and (
                table == "edinet_source_archives"
                or (path == str(case.market) and table.startswith("edinet_"))
            ):
                reads.append((path, table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    with monkeypatch.context() as guard:
        guard.setattr(sqlite3, "connect", guarded_connect)
        assert cli.main(_argv(case)) == 0, reads
    assert reads == []
    assert not any(
        query.lstrip().upper().startswith("SELECT") and "FROM edinet_source_archives" in query
        for query in queries
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert "resolved=1, reads=2" in output.out
    assert case.saved.reference in output.out
    assert case.selected_at.isoformat() in output.out
    assert case.selected_at.date() > ASOF  # The explicit historical cutoff is not receipt PIT.
    assert read_tender_offer_exit_values(case.market) == case.expected[0]
    with closing(sqlite3.connect(case.market)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edinet_source_archives").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM edinet_documents").fetchone() == (0,)
    assert [key for key in case.store.get_keys if key.endswith(".zip")] == [
        _body_key(case, RESULT),
        _body_key(case, OFFER),
    ]
    assert not {LIST_HEAD, ARCHIVE_HEAD}.intersection(case.store.get_keys + case.store.head_keys)
    assert case.store.put_keys == []
    assert case.store.values == before
    case.store.close.assert_called_once_with()


def test_saved_build_ignores_later_list_withdrawal_and_preserves_market_edinet_projection(
    tmp_path, monkeypatch
):
    case = _case(tmp_path, monkeypatch)
    receipt = next(case.lists.iter_receipts()).decoded()
    payload = json.loads(receipt.body)
    offer = next(document for document in payload["results"] if document["docID"] == OFFER)
    offer.update(legalStatus="0", withdrawalStatus="2")
    later = case.list_owner.publish(
        [
            NativeListReceipt.from_response(
                DAY,
                EdinetResponse(case.selected_at + timedelta(days=1), json.dumps(payload).encode()),
            )
        ]
    )
    materialize_lists(later, case.market)
    with closing(sqlite3.connect(case.market)) as connection:
        before = connection.execute("SELECT * FROM edinet_documents ORDER BY 1,2").fetchall()
    del case.store.values[LIST_HEAD]
    del case.store.values[ARCHIVE_HEAD]
    _clear_trace(case)
    assert cli.main(_argv(case)) == 0
    assert read_tender_offer_exit_values(case.market) == case.expected[0]
    with closing(sqlite3.connect(case.market)) as connection:
        assert (
            connection.execute("SELECT * FROM edinet_documents ORDER BY 1,2").fetchall() == before
        )
    assert not {LIST_HEAD, ARCHIVE_HEAD}.intersection(case.store.get_keys + case.store.head_keys)
    assert not case.store.put_keys


@pytest.mark.parametrize(
    "damage",
    [
        "metadata_missing",
        "body_missing",
        "body_changed",
        "missing_type5",
        "only_type1",
        "non_zip",
        "bad_csv",
        "crc",
        "no_csv",
        "bad_list",
    ],
)
def test_saved_build_source_failures_keep_old_exits_without_good_sql_fallback(
    tmp_path, monkeypatch, capsys, damage
):
    case = _case(tmp_path, monkeypatch)
    materialize_archives(
        case.archives, case.market
    )  # Good SQL bytes cannot repair the saved input.
    store_tender_offer_exit_values(case.market, case.expected[0])
    if damage == "metadata_missing":
        del case.store.values[next(key for key in case.store.values if "/selections/" in key)]
    elif damage == "body_missing":
        del case.store.values[_body_key(case)]
    elif damage == "body_changed":
        case.store.store_object(_body_key(case), b"changed")
    elif damage == "missing_type5":
        _replace_result(case, remove=True)
    elif damage == "only_type1":
        _replace_result(case, source_type=1)
    elif damage == "non_zip":
        _replace_result(case, body=b"not a ZIP")
    elif damage == "bad_csv":
        _replace_result(case, body=zipped({"XBRL_TO_CSV/report.csv": b"\xff"}))
    elif damage == "crc":
        _replace_result(
            case, body=case.archives.read_csv_zip(RESULT).replace(b"\xff\xfe", b"\xfe\xff", 1)
        )
    elif damage == "no_csv":
        _replace_result(case, body=zipped({"XBRL/report.xbrl": b"not a CSV"}))
    else:
        case.lists = case.list_owner.publish(
            [NativeListReceipt.from_response(DAY, EdinetResponse(KNOWN, b"{"))]
        )
        _save(case)
    _clear_trace(case)
    before = dict(case.store.values)
    save = Mock(side_effect=AssertionError("failed input replaced exits"))
    monkeypatch.setattr(delisting_cli, "store_tender_offer_exit_values", save)
    assert cli.main(_argv(case)) == 1
    assert capsys.readouterr().err
    save.assert_not_called()
    assert read_tender_offer_exit_values(case.market) == case.expected[0]
    assert _body_key(case, OFFER) not in case.store.get_keys
    assert not {LIST_HEAD, ARCHIVE_HEAD}.intersection(case.store.get_keys + case.store.head_keys)
    assert not case.store.put_keys
    assert case.store.values == before
    case.store.close.assert_called_once_with()


def test_valid_but_unresolved_business_evidence_keeps_existing_rejection_meaning(
    tmp_path, monkeypatch, capsys
):
    case = _case(tmp_path, monkeypatch)
    store_tender_offer_exit_values(case.market, case.expected[0])
    _replace_result(case, body=csv_zip({"test:SuccessOrFailureOfTenderOfferTextBlock": "不明"}))
    _clear_trace(case)
    assert cli.main(_argv(case)) == 0
    assert "price_or_outcome_unreadable=1" in capsys.readouterr().out
    assert read_tender_offer_exit_values(case.market) == ()
    assert not case.store.put_keys


def test_second_required_read_failure_and_retry_keep_order_without_partial_store(
    tmp_path, monkeypatch, capsys
):
    case = _case(tmp_path, monkeypatch)
    store_tender_offer_exit_values(case.market, case.expected[0])
    key = _body_key(case, OFFER)
    original = case.store.values.pop(key)
    save = Mock(wraps=store_tender_offer_exit_values)
    monkeypatch.setattr(delisting_cli, "store_tender_offer_exit_values", save)
    _clear_trace(case)
    assert cli.main(_argv(case)) == 1
    assert capsys.readouterr().err
    save.assert_not_called()
    assert read_tender_offer_exit_values(case.market) == case.expected[0]
    assert [key for key in case.store.get_keys if key.endswith(".zip")] == [
        _body_key(case, RESULT),
        _body_key(case, OFFER),
    ]
    case.store.values[key] = original
    _clear_trace(case)
    assert cli.main(_argv(case)) == 0
    save.assert_called_once()
    assert [key for key in case.store.get_keys if key.endswith(".zip")] == [
        _body_key(case, RESULT),
        _body_key(case, OFFER),
    ]
    assert not case.store.put_keys


@pytest.mark.parametrize("confirmed_empty", [False, True])
def test_no_list_receipts_are_unknown_while_a_real_empty_receipt_remains_usable(
    tmp_path, monkeypatch, confirmed_empty
):
    case = _case(tmp_path, monkeypatch)
    store_tender_offer_exit_values(case.market, case.expected[0])
    receipt = NativeListReceipt.from_response(
        DAY, EdinetResponse(KNOWN, b'{"metadata":{"resultset":{"count":0}},"results":[]}')
    )
    value = case.list_owner.publish([receipt]).value()
    value["receipts"] = [
        entry
        for entry in value["receipts"]
        if confirmed_empty and entry["receipt_id"] == receipt.receipt_id
    ]
    case.lists = FixedEdinetLists(case.store, value)
    _save(case)
    save = Mock(wraps=store_tender_offer_exit_values)
    monkeypatch.setattr(delisting_cli, "store_tender_offer_exit_values", save)
    _clear_trace(case)
    assert cli.main(_argv(case)) == int(not confirmed_empty)
    assert save.call_count == int(confirmed_empty)
    assert read_tender_offer_exit_values(case.market) == (
        () if confirmed_empty else case.expected[0]
    )
    assert not any(key.endswith(".zip") for key in case.store.get_keys)
    assert not case.store.put_keys


@pytest.mark.parametrize("damage", ["json", "hash", "format", "clock"])
def test_invalid_saved_identity_and_selection_clock_fail_without_replacement(
    tmp_path, monkeypatch, capsys, damage
):
    case = _case(tmp_path, monkeypatch)
    store_tender_offer_exit_values(case.market, case.expected[0])
    value = case.saved.model_dump(mode="json")
    if damage == "json":
        value = {}
    elif damage == "hash":
        value["sha256"] = "0" * 64
    elif damage == "format":
        value["descriptor"]["format"] = "another-input-v1"
        value["sha256"] = hashlib.sha256(
            json.dumps(value["descriptor"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        value["reference"] = "edinet-" + value["sha256"]
    else:
        # A digest-consistent selection cannot claim a receipt was already known earlier.
        key = next(key for key in case.store.values if "/selections/" in key)
        selection = json.loads(case.store.values[key].body)
        selection["selected_at"] = "2026-11-01T00:00:00+00:00"
        payload = json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        case.store.store_object("fixed-inputs/edinet/v1/selections/" + digest + ".json", payload)
        value["descriptor"]["selection_sha256"] = digest
        value["sha256"] = hashlib.sha256(
            json.dumps(value["descriptor"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        value["reference"] = "edinet-" + value["sha256"]
    case.input_file.write_text(json.dumps(value))
    _clear_trace(case)
    assert cli.main(_argv(case)) == 1
    assert capsys.readouterr().err
    assert read_tender_offer_exit_values(case.market) == case.expected[0]
    assert not case.store.put_keys
    if damage in {"json", "hash"}:
        delisting_cli.Boto3R2Store.assert_not_called()


def test_help_and_legacy_build_never_open_native_store(tmp_path, monkeypatch):
    store = Mock(side_effect=AssertionError("unexpected native store"))
    monkeypatch.setattr(delisting_cli, "Boto3R2Store", store)
    monkeypatch.setattr(cli, "load_project_env", Mock())
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build-tender-offer-exits", "--help"])
    assert exit_info.value.code == 0
    command = Mock(return_value=0)
    monkeypatch.setattr(cli, "build_tender_offer_exits_command", command)
    path = tmp_path / "market.sqlite"
    assert (
        cli.main(
            ["build-tender-offer-exits", "--sqlite-path", str(path), "--asof", ASOF.isoformat()]
        )
        == 0
    )
    assert isinstance(command.call_args.kwargs["reader"], EDINETReader)
    store.assert_not_called()


def test_missing_jpx_market_is_not_created_or_treated_as_successful_empty(
    tmp_path, monkeypatch, capsys
):
    case = _case(tmp_path, monkeypatch)
    case.market = tmp_path / "absent.sqlite"
    _clear_trace(case)
    assert cli.main(_argv(case)) == 1
    assert "TenderOfferError" in capsys.readouterr().err
    assert not case.market.exists()
    delisting_cli.Boto3R2Store.assert_not_called()
    assert not case.store.get_keys
    assert not case.store.put_keys
