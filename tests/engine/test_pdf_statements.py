"""Finite native-text contract cases; real PDF acceptance remains a separate operation."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import publish_market_fixture

from baibai_engine.data_collection.providers.jpx.models import (
    JPXAcquisitionError,
    JPXResponse,
    validate_jpx_url,
)
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.observations.financials.fixed_financial_reader import (
    read_fixed_financial_documents,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statement_reader import (
    read_pdf_statements,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    build_pdf_supplement,
    extract_pages,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.financial_pdfs import receive
from baibai_jobs.integration.jpx_handoff import export_handoff, import_handoff

STAMP = datetime(2026, 11, 1, tzinfo=UTC)
ASOF = date(2026, 10, 31)
URL = "https://www2.jpx.co.jp/disc/92680/140120260804508616.pdf"
FY_URL = "https://www2.jpx.co.jp/disc/92680/140120260511521258.pdf"


def pages(annual=False):
    if annual:
        period = "前連結会計年度\n2024年4月1日\n2025年3月31日\n当連結会計年度\n2025年4月1日\n2026年3月31日"
        table = "(連結損益計算書)"
        heading = "2026年3月期 決算短信〔IFRS〕(連結)\n2026年5月15日"
        headline = "2026年3月期 315,507 17.3 9,837 10.5 4,496 53.9 2,719 49.9 2,469 89.2 8,611 900.2\n2025年3月期 268,825 - 8,900 - 2,919 - 1,813 - 1,305 - 860 -"
        amounts = "売上高 268,825 315,507\n営業利益 8,900 9,837\n親会社の所有者 1,305 2,469"
        basis = "前連結会計年度の数値についても、IFRSベースに組み替えて比較分析"
    else:
        period = "前第1四半期連結累計期間\n2025年4月1日\n2025年6月30日\n当第1四半期連結累計期間\n2026年4月1日\n2026年6月30日"
        table = "(要約四半期連結損益計算書)"
        heading = "2027年3月期 第1四半期決算短信〔IFRS〕(連結)\n2026年8月7日"
        headline = "2027年3月期第1四半期 106,641 54.3 3,528 78.3 1,369 89.5 982 129.5 924 152.6 1,529 60.1\n2026年3月期第1四半期 69,111 - 1,979 - 722 - 428 - 366 - 955 -"
        amounts = "売上高 69,111 106,641\n営業利益 1,979 3,528\n親会社の所有者 366 924"
        basis = "前第1四半期連結累計期間の数値についても、IFRS会計基準ベースに組み替えて比較分析"
    return [
        heading
        + "\n上場会社名\nコード番号 9268\n(百万円未満切捨て)\n"
        + headline
        + "\n基本的1株当たり",
        basis,
        table + "\n(単位:百万円)\n" + period + "\n" + amounts,
    ]


def base(annual=False):
    return PdfStatement(
        ticker="9268",
        disclosure_number="20260511521258" if annual else "20260804508616",
        source_url=FY_URL if annual else URL,
        source_sha256=("a" if annual else "b") * 64,
        bundle_id="synthetic",
        resource_name="pdf:20260511521258" if annual else "pdf:20260804508616",
        received_at=STAMP,
    )


def statement(annual=False):
    return extract_pages(base(annual), pages(annual))


def test_display_precision_and_same_document_operands():
    current = statement()
    assert current.reason is None
    assert current.pairs[1].comparative == 1979
    assert current.pairs[1].current == 3528
    assert current.pairs[1].pdf_page == 3
    assert current.comparative_basis_page == 2
    result = build_pdf_supplement([statement(True), current], as_of=ASOF)
    assert {x.metric: x.amount for x in result.ttm} == {
        "sales": 353037,
        "operating_profit": 11386,
        "parent_profit": 3027,
    }
    assert all(x.period_start == date(2025, 7, 1) for x in result.ttm)
    assert all(x.precision_kind == "derived_from_truncated_display_amounts" for x in result.ttm)
    assert "context_id" not in current.model_dump_json()
    assert result.read_semantics == "retained_document_vintages_not_canonical_earnings"


@pytest.mark.parametrize(
    ("index", "old", "new", "reason"),
    [
        (0, "IFRS", "日本基準", "pdf_accounting_basis_unsupported"),
        (0, "9268", "1605", "pdf_issuer_mismatch"),
        (0, "百万円未満切捨て", "千円", "pdf_precision_unknown"),
        (1, "組み替えて", "そのまま", "pdf_comparative_basis_not_explicit"),
        (2, "2025年4月1日", "2024年4月1日", "pdf_comparative_period_mismatch"),
        (2, "単位:百万円", "単位:千円", "pdf_statement_unit_unknown"),
        (2, "1,979", "1,978", "pdf_headline_statement_conflict:operating_profit"),
        (0, "2027年3月期第1四半期", "2027年4月期第1四半期", "pdf_headline_period_conflict"),
    ],
)
def test_unverified_inputs_do_not_manufacture_numbers(index, old, new, reason):
    source = pages()
    source[index] = source[index].replace(old, new)
    value = extract_pages(base(), source)
    assert value.reason == reason
    assert not value.pairs


def test_missing_ambiguous_and_future_annual_do_not_fill_ttm():
    annual, current = statement(True), statement()
    assert not build_pdf_supplement([current], as_of=ASOF).ttm
    changed = annual.model_copy(update={"source_sha256": "c" * 64})
    assert not build_pdf_supplement([annual, changed, current], as_of=ASOF).ttm
    assert len(build_pdf_supplement([annual, annual, current], as_of=ASOF).ttm) == 3
    future = annual.model_copy(update={"published_on": date(2026, 9, 1)})
    assert not build_pdf_supplement([future, current], as_of=ASOF).ttm
    assert build_pdf_supplement([current], as_of=date(2026, 8, 6)) is None
    changed_basis = annual.model_copy(update={"accounting_standard": None})
    assert not build_pdf_supplement([changed_basis, current], as_of=ASOF).ttm


def test_publication_date_is_japanese_calendar_not_utc_day():
    captured = base().model_copy(update={"received_at": datetime(2026, 8, 6, 16, tzinfo=UTC)})
    assert extract_pages(captured, pages()).reason is None


def seed(path, monkeypatch):
    def fake_extract(**kwargs):
        annual = kwargs["source_url"] == FY_URL
        return statement(annual).model_copy(
            update={
                "bundle_id": kwargs["bundle_id"],
                "resource_name": kwargs["resource_name"],
                "received_at": kwargs["received_at"],
                "source_sha256": hashlib.sha256(kwargs["content"]).hexdigest(),
            }
        )

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.observations.financials.pdf_statement_reader.extract_pdf",
        fake_extract,
    )
    monkeypatch.setattr("baibai_jobs.integration.jpx_handoff.extract_pdf", fake_extract)
    with closing(open_connection(path)) as conn:
        resources = (("pdf:20260804508616", URL), ("pdf:20260511521258", FY_URL))
        bundle = create_bundle(conn, "financial_statements", STAMP.date(), STAMP, resources)
        for resource, url in resources:
            retain_bundle_response(
                conn,
                bundle,
                resource,
                JPXResponse(url, STAMP, "application/pdf", ("%PDF-" + url).encode()),
            )
        complete_bundle(conn, bundle)
    return bundle


def test_original_union_cutoff_and_fixed_release(tmp_path, monkeypatch):
    source, target, stage = [
        tmp_path / name for name in ("source.sqlite", "market.sqlite", "stage.sqlite")
    ]
    bundle = seed(source, monkeypatch)
    selected = export_handoff(source, stage, since=STAMP, collection="financial_statements")
    result = import_handoff(stage, target, selected, collection="financial_statements")
    assert result.complete
    assert result.report["raw_rows_added"] == 3
    assert result.report["retained_bundles"] == [bundle]
    assert (
        import_handoff(stage, target, selected, collection="financial_statements").report[
            "raw_rows_added"
        ]
        == 0
    )
    with closing(open_connection(target)) as conn:
        assert read_pdf_statements(conn, as_of=ASOF, known_at=STAMP - timedelta(seconds=1)) == {}
        assert read_pdf_statements(conn, as_of=date(2026, 5, 14)) == {}
        expected = read_pdf_statements(conn, as_of=ASOF)["9268"]
    store_jquants_fin_summaries(
        target,
        [
            {
                "Code": "92680",
                "DiscDate": "2026-08-07",
                "DiscNo": "synthetic-current",
                "DocType": "1QFinancialStatements_Consolidated_IFRS",
                "CurPerType": "1Q",
                "CurPerSt": "2026-04-01",
                "CurPerEn": "2026-06-30",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
            }
        ],
        requested_start=date(2026, 8, 7),
        requested_end=date(2026, 8, 7),
    )
    publish_market_fixture(target, "pdf-fixed", created_at=STAMP + timedelta(hours=1))
    origin = read_lake_store_origin(target)
    fixed = read_fixed_financial_documents(
        tmp_path / "mirror", origin=origin, ticker="9268", as_of=ASOF
    )
    assert fixed.pdf_statements == expected
    with closing(open_connection(target)) as conn, conn:
        conn.execute("UPDATE jpx_source_responses SET body_gzip = ?", (b"bad mutable",))
    assert (
        read_fixed_financial_documents(
            tmp_path / "mirror", origin=origin, ticker="9268", as_of=ASOF
        ).pdf_statements
        == expected
    )


def test_models_share_supplement_without_overwriting_precise_or_historical_values():
    kwargs = dict(
        ticker="9268",
        documents=(),
        adjusted_bars=(),
        adjustment_events=(),
        trading_dates=(),
        expected_price_date=None,
        sector=None,
        as_of=ASOF,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )
    old = generate_financial_and_price_models(**kwargs)
    new = generate_financial_and_price_models(
        **kwargs, pdf_statements=[statement(True), statement()]
    )
    assert new[0].payload["financial"]["pdf_supplement"] == new[1].payload["pdf_supplement"]
    assert new[0].payload["earnings_price"] == old[0].payload["earnings_price"]
    assert new[1].payload["periods"] == old[1].payload["periods"]
    assert "pdf_supplement" not in old[1].payload
    assert canonical_json(
        CompanyGrowth.model_validate(old[1].payload).model_dump(mode="json")
    ) == canonical_json(old[1].payload)


def test_receipt_import_checks_bytes_and_preserves_capture_clock(tmp_path):
    body = b"%PDF-synthetic"
    (tmp_path / "source.pdf").write_bytes(body)
    manifest = tmp_path / "receipts.json"
    record = {
        "requested_url": URL,
        "request_started_at": STAMP.isoformat(),
        "captured_at": (STAMP + timedelta(seconds=1)).isoformat(),
        "http_status": 200,
        "content_type": "application/pdf",
        "file": "source.pdf",
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    manifest.write_text(json.dumps([record]))
    result = receive(manifest, tmp_path / "handoff")
    assert result["source"] == "jpx-financial-pdfs"
    with closing(open_connection(tmp_path / "handoff" / "receipts.sqlite")) as conn:
        row = conn.execute("SELECT received_at_utc FROM jpx_source_responses").fetchone()
        assert datetime.fromisoformat(row[0]) == STAMP + timedelta(seconds=1)
    with pytest.raises(FileExistsError):
        receive(manifest, tmp_path / "handoff")
    record["sha256"] = "0" * 64
    manifest.write_text(json.dumps([record]))
    with pytest.raises(ValueError, match="bytes differ"):
        receive(manifest, tmp_path / "bad")
    validate_jpx_url(URL)
    with pytest.raises(JPXAcquisitionError, match="JPX source"):
        validate_jpx_url("https://www2.jpx.co.jp/disc/92680/other.pdf")
