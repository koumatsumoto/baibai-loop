"""固定本文の読取でもSQL一覧・受領日・sealの証拠を拡大しない。"""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_holder_capital import (
    original,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.storage_contracts import (
    EDINET_FACTS_SCOPE_DDL,
    EDINET_SOURCE_ARCHIVES_DDL,
)
from baibai_engine.machine_analysis.generation import inputs
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.disclosures.storage_contracts import (
    EDINET_DOCUMENT_LISTS_DDL,
    EDINET_DOCUMENTS_DDL,
)
from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
    read_holder_capital,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration import edinet_archive_native

DAY = date(2026, 11, 3)
KNOWN = datetime(2026, 11, 3, 12, tzinfo=UTC)


def seed(
    connection,
    *,
    issuer="1234",
    doc_id="S100TEST",
    seq=1,
    body=None,
    retained=True,
    stored_on="2026-11-02",
    source_type=1,
    edit_day=None,
):
    connection.executescript(
        EDINET_DOCUMENTS_DDL
        + EDINET_DOCUMENT_LISTS_DDL
        + EDINET_SOURCE_ARCHIVES_DDL
        + EDINET_FACTS_SCOPE_DDL
    )
    connection.execute(
        "INSERT INTO edinet_documents "
        "(doc_date,sequence_number,doc_id,sec_code,doc_type_code,xbrl_flag,legal_status,"
        "disclosure_status,withdrawal_status,doc_info_edit_status,submit_datetime) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-30", seq, doc_id, "99990", "350", "1", "1", "0", "0", "0", "2026-10-30 10:00"),
    )
    connection.execute(
        "INSERT OR IGNORE INTO edinet_document_lists "
        "(doc_date,result_count,selected_received_at_utc,latest_received_at_utc) "
        "VALUES ('2026-10-30',1,'2026-10-30T06:00:00+00:00','2026-10-30T06:00:00+00:00')"
    )
    if body is None:
        body = original(
            facts=(
                '<h:SecurityCodeOfIssuer contextRef="FilingDateInstant">'
                + issuer
                + "</h:SecurityCodeOfIssuer><h:TotalNumberOfOutstandingStocksEtc "
                'contextRef="FilingDateInstant" unitRef="u1" decimals="0">2500000'
                "</h:TotalNumberOfOutstandingStocksEtc>"
            )
        )
    if retained:
        connection.execute(
            "INSERT INTO edinet_source_archives VALUES (?,?,?,?)",
            (doc_id, source_type, stored_on, body),
        )
    if edit_day is not None:
        connection.execute(
            "INSERT INTO edinet_documents (doc_date,sequence_number,doc_id,doc_info_edit_status,doc_description) VALUES (?,1,?,'1','updated')",
            (edit_day, doc_id),
        )
        connection.execute(
            "INSERT INTO edinet_document_lists (doc_date,result_count,selected_received_at_utc,latest_received_at_utc) VALUES (?,1,?,?)",
            (edit_day, edit_day + "T06:00:00+00:00", edit_day + "T06:00:00+00:00"),
        )
    connection.commit()
    return body


def native_for(items):
    store = MemoryR2Store()
    entries = []
    for doc_id, body in items.items():
        digest = hashlib.sha256(body).hexdigest()
        store.store_object(f"l1/edinet/archives/v1/objects/{digest}.zip", body)
        entries.append(
            {
                "doc_id": doc_id,
                "source_type": 1,
                "stored_on": "2026-11-02",
                "received_at_utc": None,
                "sha256": digest,
                "size": len(body),
            }
        )
    fixed = FixedEdinetArchives(
        store,
        {
            "format": "edinet-archive-native-v1",
            "archives": entries,
            "acquisition_scope": {"since": None, "doc_ids": [], "legacy_origins": []},
        },
    )
    return store, fixed


def read(connection, *, known_at=KNOWN, as_of=DAY, seal=None, archives=None):
    return read_holder_capital(
        connection,
        as_of=as_of,
        known_at=known_at,
        tickers=("1234",),
        snapshot_sealed_at=seal,
        archives=archives,
    )


@pytest.mark.parametrize(
    "selected", [None, "invalid", "2026-10-30T06:00:00", "2026-11-03T12:00:01+00:00"]
)
def test_unproven_list_receipt_does_not_read_an_available_native_zip(selected):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc=?", (selected,)
        )
        store, archives = native_for({"S100TEST": body})
        assert read(connection, archives=archives) == ({}, "partial")
        assert store.get_keys == []


def test_receipt_after_asof_jst_is_not_proved_by_a_later_knowledge_clock():
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc='2026-11-03T15:00:00+00:00'"
        )
        store, archives = native_for({"S100TEST": body})
        assert read(connection, archives=archives, known_at=datetime(2026, 11, 4, tzinfo=UTC)) == (
            {},
            "partial",
        )
        assert store.get_keys == []


def test_selected_receipt_not_latest_and_issuer_not_filer_determine_evidence():
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc='2026-10-30T15:00:00+09:00',"
            "latest_received_at_utc='2026-11-04T20:00:00+00:00'"
        )
        expected = read(connection)
        store, archives = native_for({"S100TEST": body})
        queries = []
        connection.set_trace_callback(queries.append)
        assert read(connection, archives=archives) == expected
        observations, coverage = expected
        assert coverage == "all_retained_considered"
        fact = observations["1234"][0]
        assert fact.source_kind == "third_party_holder_report_not_issuer_attestation"
        assert fact.reference_date == date(2026, 10, 28)
        assert fact.original_sha256 == hashlib.sha256(body).hexdigest()
        assert not any("SELECT zip_content" in query for query in queries)
        assert not any(key.endswith("head.json") for key in store.head_keys + store.get_keys)


def test_other_issuers_still_contribute_to_global_coverage():
    with closing(sqlite3.connect(":memory:")) as connection:
        first = seed(connection)
        seed(connection, issuer="7777", doc_id="S100MISS", seq=2, retained=False)
        store, archives = native_for({"S100TEST": first})
        observed, coverage = read(connection, archives=archives)
        assert observed["1234"]
        assert coverage == "partial"
        assert len(store.get_keys) == 1


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_referenced_native_damage_does_not_use_the_good_sql_copy(damage):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        assert read(connection)[0]["1234"]
        store, archives = native_for({"S100TEST": body})
        key = next(iter(store.values))
        if damage == "missing":
            del store.values[key]
        else:
            store.store_object(key, b"changed", sha256=hashlib.sha256(body).hexdigest())
        with pytest.raises(LakePublishError):
            read(connection, archives=archives)
        assert read(connection)[0]["1234"]


def test_intact_invalid_zip_is_unsupported_not_native_integrity_failure():
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, body=b"not a ZIP")
        _store, archives = native_for({"S100TEST": body})
        assert read(connection, archives=archives) == read(connection) == ({}, "partial")


@pytest.mark.parametrize(
    ("seed_args", "read_args", "accepted"),
    [
        ({}, {}, True),
        ({"stored_on": "2026-11-03"}, {}, False),
        ({"stored_on": "2026-11-03"}, {"seal": "2026-11-03T07:00:00+00:00"}, True),
        ({"stored_on": "2026-11-03"}, {"seal": "2026-11-03T13:00:00+00:00"}, False),
        ({"stored_on": "2026-11-03"}, {"seal": "2026-11-03T12:00:00+00:00"}, True),
        (
            {"stored_on": "2026-11-03"},
            {"seal": "2026-11-03T16:00:00+00:00", "known_at": "2026-11-04T01:00:00+00:00"},
            False,
        ),
        (
            {"stored_on": "2026-11-04"},
            {
                "as_of": "2026-11-04",
                "known_at": "2026-11-04T08:00:00+00:00",
                "seal": "2026-11-03T07:00:00+00:00",
            },
            False,
        ),
        ({"retained": False}, {}, False),
        ({"source_type": 5}, {}, False),
        ({}, {"known_at": "2026-11-02T16:00:00+00:00"}, False),
        ({}, {"known_at": "2026-11-02T16:00:00+00:00", "seal": "2026-11-02T15:30:00+00:00"}, True),
        (
            {"stored_on": "2026-11-03", "edit_day": "2026-11-03"},
            {"seal": "2026-11-03T07:00:00+00:00"},
            False,
        ),
        ({"stored_on": "not-a-day"}, {}, False),
    ],
)
def test_sql_receipt_and_seal_remain_authoritative(seed_args, read_args, accepted):
    arguments = {
        key: date.fromisoformat(value) if key == "as_of" else datetime.fromisoformat(value)
        for key, value in read_args.items()
    }
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, **seed_args)
        store, archives = native_for({"S100TEST": body})
        expected = read(connection, **arguments)
        queries = []
        connection.set_trace_callback(queries.append)
        actual = read(connection, archives=archives, **arguments)
        assert actual == expected
        assert bool(actual[0]) is accepted
        assert actual[1] == ("all_retained_considered" if accepted else "partial")
        assert len(store.get_keys) == int(accepted)
        assert store.put_keys == []
        assert not any("SELECT zip_content" in query for query in queries)


@pytest.mark.parametrize("recorded", ["2026-11-01", "2026-11-02", "2026-11-04"])
def test_materialization_keeps_earlier_native_day_without_an_equality_gate(monkeypatch, recorded):
    uri = "file:holder-" + recorded + "?mode=memory&cache=shared"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        body = seed(connection, stored_on=recorded)
        store, archives = native_for({"S100TEST": body})
        before = connection.execute("SELECT * FROM edinet_source_archives").fetchall()
        monkeypatch.setattr(
            edinet_archive_native, "open_connection", lambda path: sqlite3.connect(uri, uri=True)
        )
        if recorded < "2026-11-02":
            with pytest.raises(ValueError, match="differs from the fixed native head"):
                edinet_archive_native.materialize_archives(archives, Path("not-opened"))
            assert connection.execute("SELECT * FROM edinet_source_archives").fetchall() == before
        else:
            result = edinet_archive_native.materialize_archives(archives, Path("not-opened"))
            assert result["copied_archives"] == 0
            assert connection.execute(
                "SELECT stored_on,zip_content FROM edinet_source_archives"
            ).fetchone() == ("2026-11-02", body)
        assert store.get_keys == store.put_keys == []


def test_day_reconciliation_changes_edit_proof_before_any_native_body_read(monkeypatch):
    uri = "file:holder-edit?mode=memory&cache=shared"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        body = seed(connection, stored_on="2026-11-04", edit_day="2026-11-03")
        store, archives = native_for({"S100TEST": body})
        args = {"as_of": date(2026, 11, 5), "known_at": datetime(2026, 11, 5, 8, tzinfo=UTC)}
        assert read(connection, **args)[0]
        monkeypatch.setattr(
            edinet_archive_native, "open_connection", lambda path: sqlite3.connect(uri, uri=True)
        )
        edinet_archive_native.materialize_archives(archives, Path("not-opened"))
        assert (
            read(connection, archives=archives, **args)
            == read(connection, **args)
            == ({}, "partial")
        )
        assert store.get_keys == []


def test_sql_presence_outside_the_fixed_native_pair_is_not_a_normal_missing_doc():
    with closing(sqlite3.connect(":memory:")) as connection:
        seed(connection)
        store, archives = native_for({})
        with pytest.raises(ValueError, match="retained EDINET archive is unavailable"):
            read(connection, archives=archives)
        assert read(connection)[0]["1234"]
        assert store.get_keys == []


@pytest.mark.parametrize(
    "mode", ["legacy", "fixed", "native_error", "input_mismatch", "late_seal", "late_evaluation"]
)
def test_generation_injects_only_the_fixed_holder_input_and_keeps_optional_failure(
    monkeypatch, caplog, mode
):
    archive_reader = object()
    late = mode.startswith("late_")
    selected = KNOWN + timedelta(seconds=1) if late else KNOWN
    fixed = (
        None if mode == "legacy" else SimpleNamespace(selected_at=selected, archives=archive_reader)
    )
    seal = KNOWN + timedelta(seconds=2) if mode == "late_evaluation" else KNOWN
    evaluation = KNOWN + timedelta(seconds=2) if mode == "late_seal" else KNOWN
    calls = []
    connection = SimpleNamespace(execute=lambda command: calls.append(command), close=lambda: None)

    class AfterHolder(Exception):
        pass

    def holder(actual, **kwargs):
        assert actual is connection
        if fixed is None:
            assert "archives" not in kwargs
            assert "selected_at" not in kwargs
        else:
            assert kwargs["archives"] is archive_reader
            assert kwargs["selected_at"] == fixed.selected_at
        assert kwargs["snapshot_sealed_at"] == seal
        assert kwargs["known_at"] == evaluation
        calls.append("holder")
        if mode == "native_error":
            raise LakePublishError("private native error")
        if mode == "input_mismatch":
            raise ValueError("private pair mismatch")
        return {}, "not_observed"

    def following(*args, **kwargs):
        raise AfterHolder

    monkeypatch.setattr(inputs, "read_master_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(inputs, "connect_read_only", lambda *a: connection)
    monkeypatch.setattr(inputs, "read_financial_documents", lambda *a, **k: [])
    monkeypatch.setattr(inputs, "read_comparison_population", lambda *a, **k: {})
    monkeypatch.setattr(inputs, "read_pdf_statements", lambda *a, **k: {})
    monkeypatch.setattr(inputs, "read_capital_actions", lambda *a, **k: {})
    monkeypatch.setattr(inputs, "read_holder_capital", holder)
    monkeypatch.setattr(inputs, "read_fixed_holder_capital", holder)
    monkeypatch.setattr(inputs, "read_price_calendar", following)
    arguments = dict(
        generation_id="holder-input",
        market_snapshot=SimpleNamespace(path=Path("/not-opened"), sealed_at=seal),
        market_release=InputRelease(release_id="fixture", manifest_sha256="a" * 64),
        economic_input=None,
        as_of=DAY,
        evaluation_time=evaluation,
        ttm_rules=DEFAULT_TTM_RULES,
        tickers=("1234",),
        edinet_input=fixed,
    )
    if late:
        with pytest.raises(ValueError, match="EDINET input was selected after"):
            inputs.read_and_generate_models(**arguments)
        assert calls == []
    else:
        with pytest.raises(AfterHolder):
            inputs.read_and_generate_models(**arguments)
        assert calls == ["BEGIN", "holder"]
    if mode in {"native_error", "input_mismatch"}:
        assert "holder-capital unavailable" in caplog.text
        assert "private" not in caplog.text
    else:
        assert "holder-capital unavailable" not in caplog.text


def test_saved_pair_uses_the_same_materialized_receipts_after_current_heads_move(tmp_path):
    import json

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
    from baibai_engine.machine_analysis.generation.edinet_inputs import (
        read_edinet_input,
        retain_edinet_input,
    )
    from baibai_jobs.integration.edinet_list_native import materialize_lists

    with TemporaryDirectory(prefix="holder-pair-", dir=tmp_path) as temporary:
        directory = Path(temporary)
        store = MemoryR2Store()
        list_owner = EdinetListL1(store, directory / "lists")
        list_owner.freeze(require_existing=False)
        documents = [
            dict(
                seqNumber=n,
                docID=doc,
                docTypeCode="350",
                xbrlFlag="1",
                legalStatus="1",
                disclosureStatus="0",
                withdrawalStatus="0",
                docInfoEditStatus="0",
                submitDateTime="2026-10-30 10:00",
            )
            for n, doc in enumerate(("S100TEST", "S100PEND"), 1)
        ]
        lists = list_owner.publish(
            [
                NativeListReceipt.from_response(
                    date(2026, 10, 30),
                    EdinetResponse(
                        datetime(2026, 10, 30, 6, tzinfo=UTC),
                        json.dumps(
                            {"metadata": {"resultset": {"count": 2}}, "results": documents}
                        ).encode(),
                    ),
                )
            ]
        )
        archive_owner = EdinetArchiveL1(store, directory / "archives")
        archive_owner.initialize()
        body = original()
        archives = archive_owner.publish(
            [
                NativeArchive.from_response(
                    "S100TEST", 1, EdinetResponse(datetime(2026, 11, 2, 6, tzinfo=UTC), body)
                )
            ]
        )
        saved = retain_edinet_input(store, lists, archives, selected_at=KNOWN)
        path = directory / "market.sqlite"
        materialize_lists(lists, path)
        edinet_archive_native.materialize_archives(archives, path)
        with closing(sqlite3.connect(path)) as connection:
            expected = read(connection)
        archive_owner.publish(
            [NativeArchive.from_response("S100PEND", 1, EdinetResponse(KNOWN, body))]
        )
        del store.values[LIST_HEAD]
        del store.values[ARCHIVE_HEAD]
        fixed = read_edinet_input(store, saved)
        before = path.read_bytes()
        before_objects = dict(store.values)
        store.get_keys.clear()
        store.head_keys.clear()

        def deny_zip_column(action, table, column, *unused):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table == "edinet_source_archives"
                and column == "zip_content"
                else sqlite3.SQLITE_OK
            )

        with closing(sqlite3.connect(path)) as connection:
            connection.set_authorizer(deny_zip_column)
            assert read(connection, archives=fixed.archives) == expected
        assert len(expected[0]["1234"]) == 1
        assert expected[1] == "partial"
        assert path.read_bytes() == before
        assert store.values == before_objects
        assert len(store.get_keys) == 1
        assert not any(key.endswith("head.json") for key in store.get_keys + store.head_keys)

        # The normal reader needs only the same pair's list projection, even on
        # a cold execution copy with no raw archive inventory or BLOBs.
        from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
            read_fixed_holder_capital,
        )

        cold = directory / "cold.sqlite"
        materialize_lists(fixed.lists, cold)
        with closing(sqlite3.connect(cold)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM edinet_source_archives").fetchone() == (
                0,
            )
        store.get_keys.clear()
        store.head_keys.clear()
        with closing(sqlite3.connect(cold)) as connection:
            queries = []
            connection.set_trace_callback(queries.append)
            assert (
                read_fixed_holder_capital(
                    connection,
                    as_of=DAY,
                    known_at=KNOWN,
                    tickers=("1234",),
                    snapshot_sealed_at=KNOWN,
                    archives=fixed.archives,
                    selected_at=fixed.selected_at,
                )
                == expected
            )
            assert not any("edinet_source_archives" in query for query in queries)
        assert fixed.identity == saved
        assert len(store.get_keys) == 1
        assert not any(key.endswith("head.json") for key in store.get_keys + store.head_keys)
        assert store.values == before_objects
