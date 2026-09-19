from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import duckdb
import pytest

from baibai_engine.market.edinet_facts.extract import extract_facts
from baibai_engine.market.edinet_facts.store import extraction_status, store_facts
from baibai_engine.market.edinet_facts.xbrl import SourceFormatError, xml_root
from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.cli.edinet_facts import (
    annual_documents,
    extract_edinet_facts_command,
)
from baibai_engine.screening.providers.edinet import EDINETProviderError
from baibai_engine.screening.providers.edinet_facts import EDINETFactsProvider

FIXTURES = Path(__file__).parents[1] / "fixtures/edinet/research"
ROOT = Path(__file__).parents[2]


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
    provider = Mock()
    provider.download_xbrl_zip.return_value = source_zip()
    kwargs = dict(asof=date(2026, 9, 18), sqlite_path=path, provider=provider)
    assert extract_edinet_facts_command(**kwargs) == 0
    provider.download_xbrl_zip.assert_not_called()
    provider.download_xbrl_zip.side_effect = EDINETProviderError("unavailable")
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 1
    assert "initialized" not in extraction_status(path)
    provider.download_xbrl_zip.side_effect = None
    assert extract_edinet_facts_command(**kwargs, initialize=True) == 0
    count = provider.download_xbrl_zip.call_count
    assert extract_edinet_facts_command(**kwargs) == 0
    assert provider.download_xbrl_zip.call_count == count
    conn = sqlite3.connect(path)
    assert conn.execute("select count(*) from edinet_segment_facts").fetchone()[0] == 40
    conn.close()


def test_new_correction_failure_preserves_original_and_does_not_reuse_it(tmp_path):
    path = tmp_path / "market.sqlite"
    original = document()
    correction = document("S100CORR", "2026-08-01 10:00", parentDocID="S100TEST")
    correction["docTypeCode"] = "130"
    insert_documents(path, [original])
    provider = Mock()
    provider.download_xbrl_zip.return_value = source_zip()
    kwargs = dict(sqlite_path=path, provider=provider)
    assert extract_edinet_facts_command(**kwargs, asof=date(2026, 7, 1), initialize=True) == 0
    insert_documents(path, [original, correction])
    provider.download_xbrl_zip.side_effect = EDINETProviderError("unavailable")
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
    sql = (ROOT / "docs/reference/queries/edinet-research-filing.sql").read_text()
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    docs = [dict(row) for row in source.execute("select * from edinet_documents")]
    source.close()
    import pyarrow as pa

    conn = duckdb.connect()
    conn.register("docs", pa.Table.from_pylist(docs))
    try:
        return conn.execute(sql, {"cutoff": cutoff, "ticker": "1873"}).fetchone()
    finally:
        conn.close()


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
    assert selected_filing(path, "2026-08-01")[-1] == "validity_unknown"
    event = document("S100EDIT", "2026-08-02 10:00", parentDocID="S100TEST")
    event.update(withdrawalStatus="1")
    insert_documents(path, [original, event])
    assert selected_filing(path, "2026-08-02")[-1] == "validity_unknown"


def test_retained_annual_family_receives_later_corrections():
    old = document()
    newer = document("S100NEXT", "2027-06-24 14:00")
    newer["periodEnd"] = "2027-03-31"
    correction = document("S100CORR", "2027-07-01 10:00", parentDocID=old["docID"])
    correction["docTypeCode"] = "130"
    selected = annual_documents([old, newer, correction], retained={old["docID"]})
    assert {d["docID"] for d in selected} == {"S100TEST", "S100NEXT", "S100CORR"}


def test_provider_raw_zip_cache_and_nonzip_rate_limit(tmp_path, monkeypatch):
    provider = EDINETFactsProvider("test-key", tmp_path)
    download = Mock(side_effect=[b'{"StatusCode":429}', source_zip()])
    monkeypatch.setattr(provider, "_request_bytes", download)
    monkeypatch.setattr("baibai_engine.screening.providers.edinet_facts.time.sleep", lambda _: None)
    data = provider.download_xbrl_zip("S100YR5P")
    assert provider.download_xbrl_zip("S100YR5P") == data
    assert download.call_count == 2
    assert "type=1" in download.call_args.args[0]
    with pytest.raises(EDINETProviderError):
        provider.download_xbrl_zip("../secret")


def test_store_keeps_vintages_and_coverage_reasons(tmp_path):
    path = tmp_path / "market.sqlite"
    one = facts()
    two = facts("S100YJ24")
    for doc, rows in [("S100YR5P", one), ("S100YJ24", two), ("S100YR5P", one)]:
        store_facts(path, doc_id=doc, disclosed_on="2026-07-22", facts=rows)
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
    provider = Mock()
    assert (
        extract_edinet_facts_command(
            asof=date(2027, 6, 25), sqlite_path=path, provider=provider, initialize=True
        )
        == 0
    )
    provider.download_xbrl_zip.assert_not_called()
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
    assert selected_filing(path, "2027-06-26")[-1] == ("validity_unknown" if blocked else "usable")
    assert [d["docID"] for d in annual_documents([old, event])] == ([] if blocked else ["S100TEST"])
