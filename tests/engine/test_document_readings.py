import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.engine.market_context.indicators.indicator_fixtures import _FakeResponse, _StaticSession

from baibai_engine.collection.economic import acquisition as acquisition
from baibai_engine.collection.economic import store as db
from baibai_engine.collection.economic.documents import (
    SourceDocument,
    read_documents,
    store_document,
)
from baibai_engine.collection.economic.providers import spglobal_pmi
from baibai_engine.collection.economic.providers.base import (
    FetchContext,
    IndicatorsProviderError,
)
from baibai_engine.collection.economic.providers.tsr_bankruptcies import TsrBankruptciesSource
from baibai_engine.collection.economic.sources import require_source
from baibai_engine.market_context import indicator_service
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading import pmi
from baibai_engine.market_context.reading.documents import read_document_series

WHEN = datetime(2026, 10, 17, tzinfo=UTC)
MONTH = date(2026, 6, 1)


def tsr_document(body):
    series = load_definitions().by_id()["jp.bankruptcies"]
    return SourceDocument(
        series.source_id,
        "history",
        series.source_url,
        "application/json",
        WHEN,
        body,
        {"params": {"data": "data"}},
    )


def test_tsr_acquires_original_bytes_without_parsing_values():
    source = require_source(load_definitions().by_id()["jp.bankruptcies"].source_id)
    body = b'{"unrecognized": "published body"}'
    result = TsrBankruptciesSource().fetch_document(
        source, session=_StaticSession(_FakeResponse(body))
    )
    assert result.payload == body
    assert result.source_id == source.source_id


def test_tsr_rejects_non_json_response():
    source = require_source(load_definitions().by_id()["jp.bankruptcies"].source_id)
    with pytest.raises(IndicatorsProviderError, match="not UTF-8 JSON"):
        TsrBankruptciesSource().fetch_document(
            source, session=_StaticSession(_FakeResponse(b"<html>not JSON</html>"))
        )


def test_tsr_l2_is_reproducible_from_original_json(tmp_path):
    series = load_definitions().by_id()["jp.bankruptcies"]
    body = json.dumps(
        [
            {"period_division": "月次", "title": "2026年6月 全国企業倒産 1,001件", "extra": None},
            {
                "period_division": "月次",
                "title": "2026年5月 全国企業倒産 999件",
                "extra": "retained",
            },
        ],
        ensure_ascii=False,
    ).encode()
    document = tsr_document(body)
    with closing(db.initialize_database(tmp_path / "data.sqlite")) as conn:
        with conn:
            store_document(conn, document)
        first = read_document_series(conn, series, start=date(2026, 5, 1), end=MONTH)
        second = read_document_series(conn, series, start=date(2026, 5, 1), end=MONTH)
        assert first == second
        assert [item.value for item in first] == [999, 1001]
        assert all(item.vintage_at == WHEN for item in first)
        assert next(read_documents(conn, series.source_id)).payload == body


def test_document_projection_failure_preserves_source_and_success(tmp_path, monkeypatch):
    path = tmp_path / "data.sqlite"
    body = b'{"shape": "changed"}'
    monkeypatch.setattr(
        acquisition, "fetch_source_documents", lambda *a, **k: iter([tsr_document(body)])
    )
    outcome = acquisition.refresh_sources(
        [tsr_document(body).source_id], db_path=path, start=MONTH, end=MONTH
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    service = indicator_service.IndicatorsService(path)
    with pytest.raises(ValueError, match="root is not a list"):
        service.get_range("jp.bankruptcies", start=MONTH, end=MONTH)
    with closing(db.open_read_only_connection(path)) as conn:
        assert next(read_documents(conn, tsr_document(body).source_id)).payload == body
        assert [
            tuple(row)
            for row in conn.execute("SELECT status, record_count FROM economic_source_runs")
        ] == [("ok", 1)]


def test_ordinary_read_neither_acquires_nor_writes(tmp_path, monkeypatch):
    path = tmp_path / "data.sqlite"
    with closing(db.initialize_database(path)):
        pass
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("read must not acquire")

    monkeypatch.setattr(acquisition, "fetch_source_documents", forbidden)
    monkeypatch.setattr(acquisition, "fetch_native_records", forbidden)
    service = indicator_service.IndicatorsService(path)
    assert service.get_range("jp.bankruptcies", start=MONTH, end=MONTH).observations == ()
    assert service.get_range("us.10y", start=MONTH, end=MONTH).observations == ()
    assert path.read_bytes() == before


def test_wrong_document_source_is_not_saved(tmp_path, monkeypatch):
    path = tmp_path / "data.sqlite"
    wrong = replace(tsr_document(b"[]"), source_id="another-source")
    monkeypatch.setattr(acquisition, "fetch_source_documents", lambda *a, **k: iter([wrong]))
    outcome = acquisition.refresh_sources(
        [tsr_document(b"[]").source_id], db_path=path, start=MONTH, end=MONTH
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    assert "another-source" in outcome.message
    with closing(db.open_read_only_connection(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 0


def test_pmi_keeps_received_pdf_when_manifest_has_a_later_gap(tmp_path, monkeypatch):
    path = tmp_path / "data.sqlite"
    url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
    release = spglobal_pmi.Release(MONTH, MONTH, url)
    monkeypatch.setattr(spglobal_pmi, "load_manifest", lambda _: {"jp_manufacturing": (release,)})
    monkeypatch.setattr(spglobal_pmi, "fetch_pdf_bytes", lambda *a, **k: b"%PDF original")
    monkeypatch.setattr(pmi, "extract_pdf_text", lambda _: "The headline PMI posted 50.4 in June.")
    source_id = load_definitions().by_id()["jp.pmi_manufacturing"].source_id
    outcome = acquisition.refresh_sources(
        [source_id], db_path=path, start=MONTH, end=date(2026, 8, 10)
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    assert "expects 2026-07-01" in outcome.message
    service = indicator_service.IndicatorsService(path)
    result = service.get_range("jp.pmi_manufacturing", start=MONTH, end=MONTH)
    assert [item.value for item in result.observations] == [50.4]
    with closing(db.open_read_only_connection(path)) as conn:
        assert next(read_documents(conn, result.series.source_id)).payload == b"%PDF original"
        assert [
            tuple(row)
            for row in conn.execute("SELECT status, record_count FROM economic_source_runs")
        ] == [("failed", 1)]


@pytest.mark.parametrize(
    ("force", "changed_url", "changed_metadata", "expected"),
    [
        (False, False, False, 1),
        (True, False, False, 1),
        (False, True, False, 1),
        (False, False, True, 1),
    ],
)
def test_pmi_rechecks_received_urls_in_ordinary_selected_range_refresh(
    monkeypatch, force, changed_url, changed_metadata, expected
):
    source = require_source("spglobal-jp-pmi-mfg")
    url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
    release = spglobal_pmi.Release(MONTH, MONTH, url)
    old_metadata = {
        "observed_at": MONTH.isoformat(),
        "release_observed_at": "2026-05-01" if changed_metadata else MONTH.isoformat(),
    }
    old = SourceDocument(
        source.source_id,
        MONTH.isoformat(),
        url + ("changed" if changed_url else ""),
        "application/pdf",
        WHEN,
        b"%PDF old",
        old_metadata,
    )
    monkeypatch.setattr(spglobal_pmi, "load_manifest", lambda _: {"jp_manufacturing": (release,)})
    fetched = []

    def fetch(url, **kwargs):
        fetched.append(url)
        return b"%PDF new"

    monkeypatch.setattr(spglobal_pmi, "fetch_pdf_bytes", fetch)
    with FetchContext() as context:
        documents = list(
            spglobal_pmi.SpGlobalPmiSource().fetch_documents(
                source,
                start=MONTH,
                end=date(2026, 7, 5),
                session=context.session,
                context=context,
                existing={old.request_key: old},
                force=force,
            )
        )
    assert len(fetched) == len(documents) == expected


def test_actual_pdf_bytes_recompute_pmi_without_a_network_request(tmp_path, monkeypatch):
    text = b"BT /F1 12 Tf 72 720 Td (The headline PMI posted 50.4 in June.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 6\n0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
    document = SourceDocument(
        series.source_id,
        MONTH.isoformat(),
        url,
        "application/pdf",
        WHEN,
        body,
        {"observed_at": MONTH.isoformat(), "release_observed_at": MONTH.isoformat()},
    )
    path = tmp_path / "data.sqlite"
    with closing(db.initialize_database(path)) as conn, conn:
        store_document(conn, document)

    def forbidden(*args, **kwargs):
        raise AssertionError("L2 read must not contact the publisher")

    monkeypatch.setattr(acquisition, "fetch_source_documents", forbidden)
    service = indicator_service.IndicatorsService(path)
    first = service.get_range(series.series_id, start=MONTH, end=MONTH)
    second = service.get_range(series.series_id, start=MONTH, end=MONTH)
    assert first.observations == second.observations
    assert first.observations[0].value == 50.4
    assert first.observations[0].vintage_at == WHEN
    assert first.observations[0].source_url == url
