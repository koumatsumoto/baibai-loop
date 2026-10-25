from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from tests.helpers.http_doubles import FakeSession, response

from baibai_engine.data_collection.edinet.archives import retain_source_archive
from baibai_engine.data_collection.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.market.edinet_facts.extract import extract_facts
from baibai_engine.market.edinet_facts.models import ExtractedFacts, filing_identity
from baibai_engine.market.edinet_facts.selection import (
    annual_documents,
    latest_annual_filing,
    usable_document,
)
from baibai_engine.market.edinet_facts.service import (
    extract_edinet_facts_command,
    refresh_edinet_facts_command,
)
from baibai_engine.market.edinet_facts.store import (
    document_inventory,
    extraction_status,
    store_facts,
)
from baibai_engine.market.edinet_facts.xbrl import SourceFormatError, xml_root
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.market.edinet_refresh import EDINETRefresh
from baibai_engine.source_maintenance import cli as app
from baibai_engine.storage.observations.schema import open_connection

FIXTURES = Path(__file__).parents[1] / "fixtures/edinet/research"


def source_zip(doc_id="S100YR5P", replace=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in FIXTURES.glob(doc_id + "*"):
            text = path.read_text()
            for old, new in (replace or {}).items():
                text = text.replace(old, new)
            archive.writestr("XBRL/PublicDoc/" + path.name, text)
    return buffer.getvalue()


def facts(doc_id="S100YR5P", replace=None):
    return extract_facts(
        content=source_zip(doc_id, replace),
        ticker="1873",
        doc_id=doc_id,
        submitted="2026-07-22 10:00",
    )


def test_source_exact_segments_periods_labels_and_principal_buckets():
    extracted = facts()
    housing = [
        r
        for r in extracted.segments
        if r.segment_name == "住宅事業" and r.period_end == "2026-04-30"
    ]
    assert {r.metric: r.value for r in housing} == {
        "external_sales": 25316000000,
        "total_sales": 25316000000,
        "segment_profit": 3331000000,
        "assets": 19734000000,
    }
    assert all(r.source_submit_datetime == "2026-07-22T10:00:00+09:00" for r in housing)
    assert {r.period_start for r in housing if r.metric != "assets"} == {"2025-05-01"}
    assert {r.period_start for r in housing if r.metric == "assets"} == {None}
    assert {r.period_end for r in extracted.segments} == {"2025-04-30", "2026-04-30"}
    hotel = [
        r
        for r in extracted.segments
        if r.segment_name == "ホテル事業" and r.period_end == "2026-04-30"
    ]
    assert {r.metric: r.value for r in hotel}["segment_profit"] == -373000000
    debt = {(r.debt_category, r.due_from_months): r.principal for r in extracted.debt}
    assert debt == {
        ("short_term_borrowings", 0): 4338000000,
        ("long_term_borrowings", 0): 432000000,
        ("long_term_borrowings", 12): 1282000000,
        ("long_term_borrowings", 24): 398000000,
        ("long_term_borrowings", 36): 392000000,
        ("long_term_borrowings", 48): 341000000,
        ("bonds", 0): 130000000,
        ("bonds", 12): 130000000,
        ("bonds", 24): 130000000,
        ("bonds", 36): 130000000,
        ("bonds", 48): 780000000,
    }
    assert all(r.currency == "JPY" and "/table[" in r.source_locator for r in extracted.debt)


def test_current_long_term_borrowing_label_keeps_the_one_year_principal():
    extracted = facts("S100YCUF")
    borrowings = [r for r in extracted.debt if r.debt_category != "bonds"]
    assert {
        (r.debt_category, r.due_from_months, r.due_to_months): r.principal for r in borrowings
    } == {
        ("short_term_borrowings", 0, 12): 15750000000,
        ("long_term_borrowings", 0, 12): 10533000000,
        ("long_term_borrowings", 12, 24): 7578000000,
        ("long_term_borrowings", 24, 36): 2715000000,
        ("long_term_borrowings", 36, 48): 2815000000,
        ("long_term_borrowings", 48, 60): 112000000,
    }
    assert {r.balance_sheet_date for r in borrowings} == {"2026-03-31"}
    assert {r.source_context for r in borrowings} == {"CurrentYearDuration"}
    assert {r.currency for r in borrowings} == {"JPY"}


def test_leases_are_excluded_and_no_bonds_does_not_become_zero():
    extracted = facts("S100YJ24")
    assert [(r.debt_category, r.principal) for r in extracted.debt] == [
        ("long_term_borrowings", 14700000000),
        ("short_term_borrowings", 18550000000),
    ]
    assert "debt_no_table" in extracted.debt_reasons


def test_profit_basis_and_aggregate_members_are_not_operating_segments():
    ordinary = facts("S100YCUF")
    assert {r.profit_basis for r in ordinary.segments if r.metric == "segment_profit"} == {
        "ordinary"
    }
    assert any(
        r.segment_kind == "total" and r.segment_key.endswith("ReportableSegmentsMember")
        for r in ordinary.segments
    )
    assert any(r.segment_kind == "other" for r in ordinary.segments)
    ifrs = facts("S100XTM0")
    alcohol = [
        r for r in ifrs.segments if r.segment_name == "酒類" and r.period_end == "2025-12-31"
    ]
    assert {r.metric: r.value for r in alcohol}["external_sales"] == 400244000000
    assert {r.profit_basis for r in alcohol if r.metric == "segment_profit"} == {"operating"}
    assert any(r.value is None for r in ifrs.segments)


def test_debt_zero_dash_and_unknown_header_are_distinct():
    original = facts()
    # Replace only the source table's numeric cell text, retaining its delimiters.
    text = (FIXTURES / "S100YR5P.xbrl").read_text()
    assert "&gt;1,282&lt;" in text
    zero = facts(replace={"&gt;1,282&lt;": "&gt;0&lt;"})
    missing = facts(replace={"&gt;1,282&lt;": "&gt;－&lt;"})

    def pick(items):
        return next(
            r.principal
            for r in items.debt
            if r.debt_category == "long_term_borrowings" and r.due_from_months == 12
        )

    assert pick(original) == 1282000000
    assert pick(zero) == 0
    assert pick(missing) is None
    unsupported = facts(replace={"1,282": "金額未定"})
    assert "debt_amount_unsupported" in unsupported.debt_reasons
    assert not any(
        r.debt_category == "long_term_borrowings" and r.due_from_months > 0
        for r in unsupported.debt
    )


@pytest.mark.parametrize(
    "content",
    [
        b'<!DOCTYPE x [<!ENTITY a "secret">]><x>&a;</x>',
        b'<a xmlns:q="urn:a"><b xmlns:q="urn:b"/></a>',
        b"<x>",
        b"\xff",
    ],
)
def test_unsupported_xml_is_rejected(content):
    with pytest.raises(SourceFormatError):
        xml_root(content)


def document(doc_id="S100TEST", submitted="2026-06-24 14:00", **kwargs):
    return dict(
        docID=doc_id,
        secCode="18730",
        docTypeCode="120",
        xbrlFlag="1",
        csvFlag="1",
        legalStatus="1",
        withdrawalStatus="0",
        disclosureStatus="0",
        docInfoEditStatus="0",
        submitDateTime=submitted,
        doc_date=submitted[:10],
        periodEnd="2026-03-31",
        **kwargs,
    )


def insert_documents(path, documents):
    aliases = {
        "docID": "doc_id",
        "secCode": "sec_code",
        "docTypeCode": "doc_type_code",
        "xbrlFlag": "xbrl_flag",
        "csvFlag": "csv_flag",
        "legalStatus": "legal_status",
        "withdrawalStatus": "withdrawal_status",
        "disclosureStatus": "disclosure_status",
        "docInfoEditStatus": "doc_info_edit_status",
        "submitDateTime": "submit_datetime",
        "parentDocID": "parent_doc_id",
        "periodEnd": "period_end",
        "docDescription": "doc_description",
        "doc_date": "doc_date",
        "opeDateTime": "operation_datetime",
    }
    conn = open_connection(path)
    with conn:
        for seq, doc in enumerate(documents, 1):
            row = {aliases[key]: value for key, value in doc.items() if key in aliases}
            row["sequence_number"] = seq
            conn.execute(
                f"INSERT OR REPLACE INTO edinet_documents ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    conn.close()


def test_initialization_is_explicit_repeat_is_idempotent_and_failure_retries(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_documents(path, [document()])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    kwargs = dict(asof=date(2026, 9, 18), sqlite_path=path, reader=reader)
    assert extract_edinet_facts_command(**kwargs) == 0
    reader.read_xbrl_zip.assert_not_called()
    reader.read_xbrl_zip.side_effect = EDINETProviderError("unavailable")
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 1
    assert "initialized" not in extraction_status(path)
    reader.read_xbrl_zip.side_effect = None
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 0
    count = reader.read_xbrl_zip.call_count
    assert extract_edinet_facts_command(**kwargs) == 0
    assert reader.read_xbrl_zip.call_count == count
    conn = sqlite3.connect(path)
    assert conn.execute("select count(*) from edinet_segment_facts").fetchone()[0] == 40
    conn.close()


def _three_annual_documents():
    documents = []
    for index, ticker in enumerate(("1873", "7203", "8306"), 1):
        filing = document(f"S100TEST{index}", f"2026-06-{23 + index:02d} 10:00")
        filing["secCode"] = ticker + "0"
        documents.append(filing)
    return documents


def _retain_xbrl(path, doc_id):
    with closing(open_connection(path)) as connection:
        return retain_source_archive(
            connection, doc_id, 1, source_zip(), stored_on=date(2026, 6, 24)
        )


def test_offline_facts_cli_uses_saved_archive_without_keys_or_http(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    insert_documents(path, [document()])
    _retain_xbrl(path, "S100TEST")
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    with (
        patch.object(app, "load_project_env", return_value=None),
        patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
        patch.object(app, "EDINETRefresh", side_effect=AssertionError("provider constructed")),
    ):
        assert (
            app.main(
                [
                    "extract-edinet-facts",
                    "--asof",
                    "2026-09-18",
                    "--initialize",
                    "--sqlite-path",
                    str(path),
                ]
            )
            == 0
        )
    assert extraction_status(path)["S100TEST"][0] == "ok"
    assert "initialized" in extraction_status(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone()[0] == 40


def test_offline_facts_cli_records_missing_archive_without_keys_or_http(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    insert_documents(path, [document()])
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    with (
        patch.object(app, "load_project_env", return_value=None),
        patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
        patch.object(app, "EDINETRefresh", side_effect=AssertionError("provider constructed")),
    ):
        assert (
            app.main(
                [
                    "extract-edinet-facts",
                    "--asof",
                    "2026-09-18",
                    "--initialize",
                    "--sqlite-path",
                    str(path),
                ]
            )
            == 1
        )
    status = extraction_status(path)
    assert status["S100TEST"][0] == "failed"
    assert "stored EDINET archive is unavailable" in (status["S100TEST"][1] or "")
    assert "initialized" not in status


def test_refresh_facts_cli_acquires_selected_ticker_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    insert_documents(path, _three_annual_documents())
    acquisition = Mock()
    acquisition.download_xbrl_zip.side_effect = lambda doc_id: _retain_xbrl(path, doc_id)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    with (
        patch.object(app, "load_project_env", return_value=None),
        patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
        patch.object(app, "EDINETRefresh", return_value=acquisition),
    ):
        assert (
            app.main(
                [
                    "refresh-edinet-facts",
                    "--asof",
                    "2026-09-18",
                    "--initialize",
                    "--ticker",
                    "7203",
                    "--sqlite-path",
                    str(path),
                ]
            )
            == 0
        )
    acquisition.download_xbrl_zip.assert_called_once_with("S100TEST2")
    acquisition.close.assert_called_once_with()
    assert set(extraction_status(path)) == {"S100TEST2"}


def test_refresh_facts_reuses_success_without_acquiring_or_reading_archive(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_documents(path, [document()])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    assert (
        extract_edinet_facts_command(
            asof=date(2026, 9, 18), sqlite_path=path, reader=reader, initialize=True
        )
        == 0
    )
    acquisition = Mock()
    offline_reader = Mock()
    output = io.StringIO()
    assert (
        refresh_edinet_facts_command(
            asof=date(2026, 9, 18),
            sqlite_path=path,
            reader=offline_reader,
            acquisition=acquisition,
            stdout=output,
        )
        == 0
    )
    acquisition.download_xbrl_zip.assert_not_called()
    offline_reader.read_xbrl_zip.assert_not_called()
    assert json.loads(output.getvalue())["edinet_research_facts"]["reused"] == 1


def test_research_facts_continue_after_one_acquisition_failure(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_documents(path, _three_annual_documents())
    provider = Mock()
    requested = []

    def download(doc_id):
        requested.append(doc_id)
        if doc_id == "S100TEST2":
            raise EdinetAcquisitionError("unavailable")
        return _retain_xbrl(path, doc_id)

    provider.download_xbrl_zip.side_effect = download
    output = io.StringIO()
    assert (
        refresh_edinet_facts_command(
            asof=date(2026, 9, 18),
            sqlite_path=path,
            reader=EDINETReader(path),
            acquisition=provider,
            initialize=True,
            stdout=output,
        )
        == 1
    )
    assert requested == ["S100TEST1", "S100TEST2", "S100TEST3"]
    statuses = extraction_status(path)
    assert [statuses[doc_id][0] for doc_id in requested] == ["ok", "failed", "ok"]
    assert "initialized" not in statuses
    with sqlite3.connect(path) as conn:
        saved = conn.execute(
            "SELECT DISTINCT source_doc_id FROM edinet_segment_facts ORDER BY source_doc_id"
        ).fetchall()
    assert saved == [("S100TEST1",), ("S100TEST3",)]
    assert json.loads(output.getvalue())["edinet_research_facts"]["extracted"] == 2


def test_research_facts_stop_after_rate_limit_without_planning_later_documents(
    tmp_path, monkeypatch
):
    path = tmp_path / "market.sqlite"
    insert_documents(path, _three_annual_documents())
    provider = Mock()
    requested = []
    planned = []

    from baibai_engine.market.edinet_facts import service

    original_plan = service.plan_fact_item

    def track_plan(doc, **kwargs):
        planned.append(doc["docID"])
        return original_plan(doc, **kwargs)

    def download(doc_id):
        requested.append(doc_id)
        if doc_id == "S100TEST2":
            raise EdinetRateLimitError("rate limited")
        return _retain_xbrl(path, doc_id)

    monkeypatch.setattr(service, "plan_fact_item", track_plan)
    provider.download_xbrl_zip.side_effect = download
    output = io.StringIO()
    assert (
        refresh_edinet_facts_command(
            asof=date(2026, 9, 18),
            sqlite_path=path,
            reader=EDINETReader(path),
            acquisition=provider,
            initialize=True,
            stdout=output,
        )
        == 1
    )
    assert planned == requested == ["S100TEST1", "S100TEST2"]
    statuses = extraction_status(path)
    assert statuses["S100TEST1"][0] == "ok"
    assert statuses["S100TEST2"][0] == "failed"
    assert "S100TEST3" not in statuses
    assert "initialized" not in statuses
    with sqlite3.connect(path) as conn:
        saved = conn.execute("SELECT DISTINCT source_doc_id FROM edinet_segment_facts").fetchall()
    assert saved == [("S100TEST1",)]
    counts = json.loads(output.getvalue())["edinet_research_facts"]
    assert counts["failed"] == counts["rate_limited"] == 1


def test_research_facts_ticker_limit_does_not_initialize_global_baseline(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_documents(path, _three_annual_documents())
    provider = Mock()
    provider.download_xbrl_zip.side_effect = lambda doc_id: _retain_xbrl(path, doc_id)
    assert (
        refresh_edinet_facts_command(
            asof=date(2026, 9, 18),
            sqlite_path=path,
            reader=EDINETReader(path),
            acquisition=provider,
            initialize=True,
            tickers=("7203",),
        )
        == 0
    )
    provider.download_xbrl_zip.assert_called_once_with("S100TEST2")
    statuses = extraction_status(path)
    assert set(statuses) == {"S100TEST2"}
    assert statuses["S100TEST2"][0] == "ok"


def test_new_correction_failure_preserves_original_and_does_not_reuse_it(tmp_path):
    path = tmp_path / "market.sqlite"
    original = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction["docTypeCode"] = "130"
    insert_documents(path, [original])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    kwargs = dict(sqlite_path=path, reader=reader)
    assert extract_edinet_facts_command(**kwargs, asof=date(2026, 7, 1), initialize=True) == 0
    insert_documents(path, [original, correction])
    reader.read_xbrl_zip.side_effect = EDINETProviderError("unavailable")
    assert extract_edinet_facts_command(**kwargs, asof=date(2026, 8, 2)) == 1
    assert extraction_status(path)["S100CORR"][0] == "failed"
    assert selected_filing(path, "2026-07-31")[0] == "S100TEST"
    assert selected_filing(path, "2026-08-02")[0] == "S100CORR"
    conn = sqlite3.connect(path)
    assert conn.execute("select distinct source_doc_id from edinet_segment_facts").fetchall() == [
        ("S100TEST",)
    ]
    conn.close()


def selected_filing(path, cutoff):
    doc = latest_annual_filing(document_inventory(path, date.fromisoformat(cutoff)), "1873")
    if doc is None:
        return None
    return (
        doc["docID"],
        doc.get("submitDateTime"),
        doc["_research_annual_period_end"],
        "usable" if usable_document(doc) else "validity_unknown",
    )


def test_query_selects_latest_before_validity_and_handles_events(tmp_path):
    path = tmp_path / "market.sqlite"
    original = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction.update(docTypeCode="130", periodEnd=None, xbrlFlag="0")
    insert_documents(path, [original, correction])
    assert selected_filing(path, "2026-06-23") is None
    assert selected_filing(path, "2026-07-31")[-1] == "usable"
    assert selected_filing(path, "2026-08-01") == (
        "S100CORR",
        "2026-08-01 10:00",
        "2026-03-31",
        "validity_unknown",
    )
    correction.update(xbrlFlag="1", parentDocID="S100LOST")
    insert_documents(path, [original, correction])
    assert selected_filing(path, "2026-08-01") is None
    assert annual_documents([original, correction]) == []
    event = document("S100EDIT", "2026-08-02 10:00", parentDocID="S100TEST")
    event.update(withdrawalStatus="1")
    insert_documents(path, [original, event])
    assert selected_filing(path, "2026-08-02") is None


def test_query_and_runtime_order_duplicate_origins_by_operation_time(tmp_path):
    path = tmp_path / "market.sqlite"
    later = document()
    later.update(seqNumber=1, opeDateTime="2026-06-24 16:00", periodEnd="2026-03-31")
    earlier = document()
    earlier.update(seqNumber=2, opeDateTime="2026-06-24 15:00", periodEnd="2025-03-31")
    insert_documents(path, [later, earlier])

    selected = selected_filing(path, "2026-06-24")
    assert selected == ("S100TEST", "2026-06-24 14:00", "2026-03-31", "usable")
    assert [row["periodEnd"] for row in annual_documents([later, earlier])] == ["2026-03-31"]


def test_retained_annual_family_receives_later_corrections():
    old = document()
    newer = document("S100NEXT", "2027-06-24 14:00")
    newer["periodEnd"] = "2027-03-31"
    correction = document("S100CORR", "2027-07-01 10:00", parentDocID=old["docID"])
    correction["docTypeCode"] = "130"
    selected = annual_documents([old, newer, correction], retained={old["docID"]})
    assert {d["docID"] for d in selected} == {"S100TEST", "S100NEXT", "S100CORR"}


def test_provider_raw_zip_cache_and_nonzip_rate_limit(tmp_path, monkeypatch):
    session = FakeSession(response(b'{"StatusCode":429}'), response(source_zip()))
    provider = EDINETRefresh("test-key", sqlite_path=tmp_path / "market.sqlite", session=session)
    monkeypatch.setattr("baibai_engine.data_collection.edinet.http.time.sleep", lambda _: None)
    data = provider.download_xbrl_zip("S100YR5P")
    assert provider.download_xbrl_zip("S100YR5P") == data
    assert len(session.calls) == 2
    assert "type=1" in session.calls[-1]
    with pytest.raises(EDINETProviderError):
        provider.download_xbrl_zip("../secret")


def test_store_keeps_vintages_and_coverage_reasons(tmp_path):
    path = tmp_path / "market.sqlite"
    one = facts()
    two = facts("S100YJ24")
    for doc, rows in [("S100YR5P", one), ("S100YJ24", two), ("S100YR5P", one)]:
        store_facts(path, identity=filing_identity("1873", doc, "2026-07-22 10:00"), facts=rows)
    conn = sqlite3.connect(path)
    assert conn.execute("select count(*) from edinet_segment_facts").fetchone()[0] == 72
    conn.close()
    assert json.loads(extraction_status(path)["S100YJ24"][1])["debt_reasons"] == ["debt_no_table"]


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_dtd_cannot_bypass_guard_via_alternate_encoding(encoding):
    content = '<!DOCTYPE x [<!ENTITY a "EXPANDED">]><x>&a;</x>'.encode(encoding)
    with pytest.raises(SourceFormatError, match="xml_encoding_unsupported"):
        xml_root(content)


def test_new_annual_unknown_period_blocks_old_filing_and_records_reason(tmp_path):
    path = tmp_path / "market.sqlite"
    old = document()
    new = document("S100NEXT", "2027-06-24 14:00")
    new["periodEnd"] = None
    insert_documents(path, [old, new])
    assert selected_filing(path, "2027-06-25")[-1] == "validity_unknown"
    selected = annual_documents([old, new])
    assert [d["docID"] for d in selected] == ["S100NEXT"]
    reader = Mock()
    assert (
        extract_edinet_facts_command(
            asof=date(2027, 6, 25), sqlite_path=path, reader=reader, initialize=True
        )
        == 0
    )
    reader.read_xbrl_zip.assert_not_called()
    assert extraction_status(path)["S100NEXT"] == ("failed", "annual_period_unknown")


def test_correction_inherits_parent_identity_and_period(tmp_path):
    path = tmp_path / "market.sqlite"
    old = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction.update(docTypeCode="130", periodEnd=None, secCode=None)
    insert_documents(path, [old, correction])
    assert selected_filing(path, "2026-08-02")[0] == "S100CORR"
    assert selected_filing(path, "2026-08-02")[-1] == "usable"
    selected = annual_documents([old, correction])
    assert selected[-1]["secCode"] == "18730"
    assert selected[-1]["_research_period_known"]


@pytest.mark.parametrize(
    ("doc_type", "event_date", "parent", "blocked"),
    [
        ("120", "2027-06-25", None, True),
        ("130", "2027-06-25", None, True),
        ("350", "2027-06-25", None, False),
        ("350", "2027-06-25", "S100TEST", True),
        ("120", "2025-06-25", None, False),
    ],
)
def test_unresolved_event_only_blocks_a_relevant_newer_annual(
    tmp_path, doc_type, event_date, parent, blocked
):
    path = tmp_path / "market.sqlite"
    old = document()
    event = document("S100NEXT", event_date + " 10:00")
    event.update(docTypeCode=doc_type, docInfoEditStatus="1", periodEnd=None, parentDocID=parent)
    insert_documents(path, [old, event])
    selected = selected_filing(path, "2027-06-26")
    if blocked:
        assert selected is None
    else:
        assert selected[-1] == "usable"
    assert [d["docID"] for d in annual_documents([old, event])] == ([] if blocked else ["S100TEST"])


def test_event_linked_to_old_family_keeps_new_annual_usable(tmp_path):
    path = tmp_path / "market.sqlite"
    old = document()
    new = document("S100NEW1", "2027-06-24 10:00")
    new.update(periodEnd="2027-03-31")
    event = document("S100EVNT", "2027-07-01 10:00")
    event.update(
        docTypeCode="350",
        docInfoEditStatus="1",
        parentDocID="S100TEST",
        periodEnd=None,
    )
    insert_documents(path, [old, event])
    assert selected_filing(path, "2027-07-02") is None
    assert annual_documents([old, event], retained={"S100TEST"}) == []
    insert_documents(path, [new])
    selected = selected_filing(path, "2027-07-02")
    assert (selected[0], selected[-1]) == ("S100NEW1", "usable")
    assert [d["docID"] for d in annual_documents([old, new, event], retained={"S100TEST"})] == [
        "S100NEW1"
    ]


def test_operation_cannot_introduce_a_cycle_before_annual_family_selection():
    original = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction["docTypeCode"] = "130"
    event = {**correction, "doc_date": "2026-08-02", "docInfoEditStatus": "1"}
    event["parentDocID"] = "S100CORR"

    with pytest.raises(EDINETProviderError, match="parent relation cycle"):
        annual_documents([original, correction, event])


def test_withdrawal_uses_parent_links_after_operation_metadata_edits():
    from baibai_engine.market.edinet_document_events import _canonicalize_document_events

    original = document()
    newer = document("S100NEW1", "2027-06-24 10:00")
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction["docTypeCode"] = "130"
    edit = {**correction, "doc_date": "2027-07-01", "docInfoEditStatus": "1"}
    edit["parentDocID"] = "S100NEW1"
    withdrawal = document("S100WTHD", "2027-07-02 10:00", parentDocID="S100NEW1")
    withdrawal.update(docTypeCode="350", withdrawalStatus="1")

    folded, quarantined = _canonicalize_document_events(
        [original, newer, correction, edit, withdrawal], origin_start=None
    )
    by_id = {row["docID"]: row for row in folded}
    assert not quarantined
    assert by_id["S100NEW1"]["withdrawalStatus"] == "2"
    assert by_id["S100CORR"]["withdrawalStatus"] == "2"
    assert by_id["S100TEST"]["withdrawalStatus"] == "0"


@pytest.mark.parametrize("include_withdrawal", [False, True])
def test_refreshed_tombstone_allows_earlier_metadata_without_reviving_document(
    include_withdrawal,
):
    from baibai_engine.market.edinet_document_events import _canonicalize_document_events

    original = document()
    original.update(withdrawalStatus="2", legalStatus="0", docInfoEditStatus="2")
    edit = document()
    edit.update(
        doc_date="2026-07-01",
        docInfoEditStatus="1",
        opeDateTime="2026-07-01 10:00",
        periodEnd="2026-04-30",
    )
    withdrawal = document("S100WTHD", "2026-07-02 10:00", parentDocID="S100TEST")
    withdrawal.update(docTypeCode="350", withdrawalStatus="1")
    inputs = [original, edit, *([withdrawal] if include_withdrawal else [])]

    folded, quarantined = _canonicalize_document_events(inputs, origin_start=None)

    assert not quarantined
    assert len(folded) == 1
    assert folded[0]["periodEnd"] == "2026-04-30"
    assert folded[0]["withdrawalStatus"] == "2"
    assert folded[0]["legalStatus"] == "0"
    assert original["periodEnd"] == "2026-03-31"


@pytest.mark.parametrize("through_sqlite", [False, True])
def test_metadata_edit_clears_optional_values_in_raw_and_stored_rows(tmp_path, through_sqlite):
    from tests.helpers.edinet_documents import store_edinet_documents

    from baibai_engine.market.edinet_document_events import _canonicalize_document_events
    from baibai_engine.market.edinet_metrics.store import read_edinet_documents

    original = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction["docTypeCode"] = "130"
    correction["docInfoEditStatus"] = "2"
    edit = {**correction, "doc_date": "2026-08-02", "docInfoEditStatus": "1"}
    edit.update(parentDocID=None, periodEnd=None)
    documents = [original, correction, edit]
    if through_sqlite:
        path = tmp_path / "market.sqlite"
        documents = []
        for row in (original, correction, edit):
            day = date.fromisoformat(row["doc_date"])
            native = {key: value for key, value in row.items() if key != "doc_date"}
            native["seqNumber"] = 1
            store_edinet_documents(
                path,
                day,
                [native],
                response_json=json.dumps(
                    {"metadata": {"resultset": {"count": 1}}, "results": [native]}
                ),
            )
            stored = read_edinet_documents(path, day)
            assert stored is not None
            documents.extend(stored)
    folded, quarantined = _canonicalize_document_events(documents, origin_start=None)
    current = next(row for row in folded if row["docID"] == "S100CORR")
    assert not quarantined
    assert current["parentDocID"] is None
    assert current["periodEnd"] is None
    assert current["docTypeCode"] == "130"
    assert correction["parentDocID"] == "S100TEST"
    assert correction["periodEnd"] == "2026-03-31"


@pytest.mark.parametrize("edited", ["0", "2"])
def test_metadata_edit_status_is_not_document_unavailability(tmp_path, edited):
    from baibai_engine.market.edinet_selection import select_document_candidates

    # EDINET API v2 section 3-1-6: 2 identifies the edited filing, not a withdrawal.
    original = document()
    original["docInfoEditStatus"] = edited
    path = tmp_path / "market.sqlite"
    insert_documents(path, [original])
    assert selected_filing(path, "2026-06-24")[-1] == "usable"
    assert usable_document(annual_documents([original])[0])
    assert select_document_candidates([original]).candidates["1873"].doc_id == "S100TEST"


@pytest.mark.parametrize("field", ["legalStatus", "disclosureStatus", "withdrawalStatus"])
@pytest.mark.parametrize("missing", [None, "", "omitted"])
def test_missing_document_validity_is_not_treated_as_legacy_usable(field, missing):
    from baibai_engine.market.edinet_selection import select_document_candidates

    original = document()
    if missing == "omitted":
        original.pop(field)
    else:
        original[field] = missing
    assert not select_document_candidates([original]).candidates
    assert not usable_document(annual_documents([original])[0])


def test_orphan_correction_does_not_block_a_later_annual_family():
    old = document()
    orphan = document("S100CORR", "2026-08-01 10:00", parentDocID="S100LOST")
    orphan["docTypeCode"] = "130"
    newer = document("S100NEXT", "2027-06-24 10:00")
    newer["periodEnd"] = "2027-03-31"
    assert [row["docID"] for row in annual_documents([old, orphan, newer])] == ["S100NEXT"]


@pytest.mark.parametrize("refreshed", [False, True])
def test_saved_selection_resolves_metadata_edit_without_permanent_rejection(tmp_path, refreshed):
    path = tmp_path / "market.sqlite"
    original = document()
    edit = {
        **original,
        "doc_date": "2026-07-01",
        "docInfoEditStatus": "1",
        "opeDateTime": "2026-07-01 10:00",
        "periodEnd": "2026-04-30",
    }
    documents = [original, edit]
    if refreshed:
        documents.append({**edit, "doc_date": "2026-07-02", "docInfoEditStatus": "2"})
    insert_documents(path, documents)
    assert selected_filing(path, "2026-06-30") == (
        "S100TEST",
        "2026-06-24 14:00",
        "2026-03-31",
        "usable",
    )
    for cutoff in ["2026-07-01", "2026-07-02"]:
        assert selected_filing(path, cutoff) == (
            "S100TEST",
            "2026-06-24 14:00",
            "2026-04-30",
            "usable",
        )


@pytest.mark.parametrize("description_period", [False, True])
@pytest.mark.parametrize("orphan_end", ["2022-03-31", "2024-03-31", "2025-03-31", None])
def test_orphan_correction_is_scoped_to_its_fiscal_period(tmp_path, description_period, orphan_end):
    # Saved EDINET shape: a 2024 filing and correction coexist with a later
    # correction of the 2022 report whose parent predates retained coverage.
    original = document("S100TXF4", "2024-06-28 10:31")
    original["periodEnd"] = "2024-03-31"
    linked = document("S100UZ4I", "2024-12-20 11:36", parentDocID="S100TXF4")
    linked.update(docTypeCode="130", periodEnd=None)
    orphan = document("S100UZ44", "2024-12-20 11:11", parentDocID="S100OLL4")
    orphan.update(docTypeCode="130", periodEnd=orphan_end)
    if description_period:
        orphan["periodEnd"] = None
        if orphan_end is not None:
            year = int(orphan_end[:4])
            orphan["docDescription"] = f"訂正有価証券報告書－第44期({year - 1}/04/01－{year}/03/31)"
    documents = [original, linked, orphan]
    expected = ["S100TXF4", "S100UZ4I"] if orphan_end == "2022-03-31" else []
    assert [row["docID"] for row in annual_documents(documents)] == expected
    path = tmp_path / "market.sqlite"
    insert_documents(path, documents)
    selected = selected_filing(path, "2024-12-21")
    if expected:
        assert selected == ("S100UZ4I", "2024-12-20 11:36", "2024-03-31", "usable")
    else:
        assert selected is None


@pytest.mark.parametrize(
    ("field", "value"), [("secCode", "99990"), ("submitDateTime", "2026-06-24 15:00")]
)
def test_research_facts_reextract_when_document_identity_changes(tmp_path, field, value):
    path = tmp_path / "market.sqlite"
    original = document()
    insert_documents(path, [original])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    kwargs = dict(asof=date(2026, 9, 18), sqlite_path=path, reader=reader)
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 0
    changed = {**original, field: value}
    insert_documents(path, [changed])
    assert extract_edinet_facts_command(**kwargs) == 0
    expected = filing_identity(changed["secCode"][:4], changed["docID"], changed["submitDateTime"])
    with sqlite3.connect(path) as conn:
        for table in ("edinet_segment_facts", "edinet_debt_schedule"):
            assert conn.execute(
                f"SELECT DISTINCT ticker, source_submit_datetime FROM {table}"
            ).fetchall() == [(expected.ticker, expected.source_submit_datetime)]
    assert reader.read_xbrl_zip.call_count == 2
    assert extract_edinet_facts_command(**kwargs) == 0
    assert reader.read_xbrl_zip.call_count == 2


def test_research_facts_identity_uses_normalized_submission_time(tmp_path):
    path = tmp_path / "market.sqlite"
    original = document()
    insert_documents(path, [original])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    kwargs = dict(asof=date(2026, 9, 18), sqlite_path=path, reader=reader)
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 0
    insert_documents(path, [{**original, "submitDateTime": "2026-06-24T14:00:00+09:00"}])
    assert extract_edinet_facts_command(**kwargs) == 0
    assert reader.read_xbrl_zip.call_count == 1


def test_empty_research_facts_still_bind_the_extraction_identity(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    original = document()
    insert_documents(path, [original])
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    monkeypatch.setattr(
        "baibai_engine.market.edinet_facts.service.extract_facts",
        lambda **kwargs: ExtractedFacts((), ()),
    )
    kwargs = dict(asof=date(2026, 9, 18), sqlite_path=path, reader=reader)
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 0
    assert extract_edinet_facts_command(**kwargs) == 0
    assert reader.read_xbrl_zip.call_count == 1
    insert_documents(path, [{**original, "secCode": "99990"}])
    assert extract_edinet_facts_command(**kwargs) == 0
    assert reader.read_xbrl_zip.call_count == 2


@pytest.mark.parametrize("period_end", ["2027-03-31", None])
def test_quarantined_latest_family_cannot_make_previous_annual_current(tmp_path, period_end):
    path = tmp_path / "market.sqlite"
    old = document()
    new = document("S100NEW1", "2027-06-24 10:00")
    new["periodEnd"] = period_end
    event = document("S100EVNT", "2027-07-01 10:00")
    event.update(docTypeCode="350", docInfoEditStatus="1", parentDocID="S100NEW1", periodEnd=None)
    insert_documents(path, [old, new])
    assert selected_filing(path, "2027-06-30")[0] == "S100NEW1"
    insert_documents(path, [event])
    assert annual_documents([old, new, event]) == []
    assert latest_annual_filing([old, new, event], "1873") is None
    assert selected_filing(path, "2027-07-02") is None
    # Explicit retained history can still be maintained; it is not selected as current.
    assert [
        row["docID"] for row in annual_documents([old, new, event], retained={old["docID"]})
    ] == [old["docID"]]
