import io
import zipfile
from datetime import UTC, datetime

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    SourceFormatError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
    extract_holder_capital,
)


def original(*, facts=None, unit="xbrli:shares", period="2026-10-28"):
    if facts is None:
        facts = '<h:SecurityCodeOfIssuer contextRef="FilingDateInstant">1234</h:SecurityCodeOfIssuer><h:TotalNumberOfOutstandingStocksEtc contextRef="FilingDateInstant" unitRef="u1" decimals="0">2500000</h:TotalNumberOfOutstandingStocksEtc>'
    xml = f"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:h="http://disclosure.edinet-fsa.go.jp/taxonomy/jplvh/2025-11-01/jplvh_cor"><xbrli:context id="FilingDateInstant"><xbrli:entity><xbrli:identifier scheme="urn:edinet">E00001-000</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>{period}</xbrli:instant></xbrli:period></xbrli:context><xbrli:unit id="u1"><xbrli:measure>{unit}</xbrli:measure></xbrli:unit>{facts}</xbrli:xbrl>"""
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("XBRL/PublicDoc/report.xbrl", xml)
    return target.getvalue()


def read(
    content,
    *,
    submitted=datetime(2026, 10, 30, tzinfo=UTC),
    known=datetime(2026, 11, 3, tzinfo=UTC),
):
    return extract_holder_capital(
        content, document_id="S100TEST", submitted_at=submitted, known_at=known
    )


def test_reports_source_date_and_third_party_scope():
    result = read(original())
    assert result is not None
    assert (result.ticker, result.total_stocks, result.reference_date.isoformat()) == (
        "1234",
        2500000,
        "2026-10-28",
    )
    assert result.source_kind == "third_party_holder_report_not_issuer_attestation"


def test_future_filing_and_wrong_unit_are_not_observations():
    assert read(original(), known=datetime(2026, 10, 29, tzinfo=UTC)) is None
    assert read(original(unit="xbrli:pure")) is None
    assert read(original(period="2026-11-01")) is None


def test_conflicting_or_nonstandard_facts_are_not_filled():
    facts = '<h:SecurityCodeOfIssuer contextRef="FilingDateInstant">1234</h:SecurityCodeOfIssuer><h:TotalNumberOfOutstandingStocksEtc contextRef="FilingDateInstant" unitRef="u1" decimals="0">2500000</h:TotalNumberOfOutstandingStocksEtc><h:TotalNumberOfOutstandingStocksEtc contextRef="FilingDateInstant" unitRef="u1" decimals="0">2600000</h:TotalNumberOfOutstandingStocksEtc>'
    assert read(original(facts=facts)) is None
    assert read(original(facts="")) is None


def test_xml_dtd_is_rejected():
    with pytest.raises(SourceFormatError):
        read(original(facts='<!DOCTYPE bad [<!ENTITY x "1">]>'))


def test_reader_respects_retention_day_and_reports_partial_coverage(tmp_path):
    import json
    from contextlib import closing
    from datetime import date

    from tests.helpers.edinet_documents import store_edinet_documents

    from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
    from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
        read_holder_capital,
    )
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection

    path = tmp_path / "market.sqlite"
    docs = [
        {
            "seqNumber": 1,
            "docID": "S100TEST",
            "docTypeCode": "350",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "disclosureStatus": "0",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "submitDateTime": "2026-10-30 10:00",
        },
        {
            "seqNumber": 2,
            "docID": "S100PENDING",
            "docTypeCode": "350",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "disclosureStatus": "0",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "submitDateTime": "2026-10-30 11:00",
        },
    ]
    store_edinet_documents(
        path,
        date(2026, 10, 30),
        docs,
        response_json=json.dumps(
            {"metadata": {"resultset": {"count": len(docs)}}, "results": docs}
        ),
    )
    with closing(open_connection(path)) as connection:
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc=? WHERE doc_date=?",
            ("2026-10-30T06:00:00+00:00", "2026-10-30"),
        )
        connection.commit()
        retain_source_archive(connection, "S100TEST", 1, original(), stored_on=date(2026, 11, 2))
        queries: list[str] = []
        connection.set_trace_callback(queries.append)
        result, coverage = read_holder_capital(
            connection,
            as_of=date(2026, 11, 3),
            known_at=datetime(2026, 11, 3, 12, tzinfo=UTC),
            tickers=("1234",),
        )
        assert result["1234"][0].total_stocks == 2500000
        assert coverage == "partial"
        blob_reads = [query for query in queries if "SELECT zip_content" in query]
        assert len(blob_reads) == 1 and "S100TEST" in blob_reads[0]
        historical, _ = read_holder_capital(
            connection,
            as_of=date(2026, 11, 2),
            known_at=datetime(2026, 11, 3, 12, tzinfo=UTC),
            tickers=("1234",),
        )
        assert historical == {}
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc=? WHERE doc_date=?",
            ("2026-11-03T06:00:00+00:00", "2026-10-30"),
        )
        connection.commit()
        before_list_received, coverage = read_holder_capital(
            connection,
            as_of=date(2026, 11, 3),
            known_at=datetime(2026, 11, 3, 5, tzinfo=UTC),
            tickers=("1234",),
        )
        assert before_list_received == {}
        assert coverage == "partial"
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc=? WHERE doc_date=?",
            ("2026-10-30T06:00:00+00:00", "2026-10-30"),
        )
        connection.execute(
            "UPDATE edinet_source_archives SET stored_on=? WHERE doc_id=?",
            ("2026-11-03", "S100TEST"),
        )
        connection.commit()
        proven, _ = read_holder_capital(
            connection,
            as_of=date(2026, 11, 3),
            known_at=datetime(2026, 11, 3, 8, tzinfo=UTC),
            snapshot_sealed_at=datetime(2026, 11, 3, 7, tzinfo=UTC),
            tickers=("1234",),
        )
        assert proven["1234"][0].total_stocks == 2500000
        unproven, _ = read_holder_capital(
            connection,
            as_of=date(2026, 11, 3),
            known_at=datetime(2026, 11, 3, 6, tzinfo=UTC),
            snapshot_sealed_at=datetime(2026, 11, 3, 7, tzinfo=UTC),
            tickers=("1234",),
        )
        assert unproven == {}
        earlier_asof, _ = read_holder_capital(
            connection,
            as_of=date(2026, 11, 2),
            known_at=datetime(2026, 11, 3, 8, tzinfo=UTC),
            snapshot_sealed_at=datetime(2026, 11, 3, 7, tzinfo=UTC),
            tickers=("1234",),
        )
        assert earlier_asof == {}
