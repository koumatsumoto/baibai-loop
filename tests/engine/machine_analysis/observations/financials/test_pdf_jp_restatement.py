"""合成の日本基準遡及表。原本取得・本番受入の代用ではない。"""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    select_pdf_financials,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    build_pdf_supplement,
    extract_pages,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

ASOF = date(2026, 11, 3)
CURRENT = """2027年3月期 第1四半期決算短信〔日本基準〕(連結)
2026年8月5日
上場会社名 テスト
コード番号 9127
(百万円未満切捨て)
1.2027年3月期第1四半期の連結業績（2026年4月1日～2026年6月30日）
売上高 営業利益 経常利益 親会社株主に帰属する四半期純利益
2027年3月期第1四半期 1,473 46.6 338 ― 368 ― 247 ―
2026年3月期第1四半期 1,004 △29.8 △36 ― △127 ― △61 ―
(注)その他
(注)2026年3月期第1四半期の数値は、会計方針の変更に伴い遡及修正が行われたため、遡及後の数値に変更しております。"""
REFERENCE = """2026年8月5日
各位
通期連結業績予想値の修正
(単位:百万円)
売上高 営業利益 経常利益 親会社株主に帰属する当期純利益
今回発表予想 5,700 820 840 560 366円02銭
(ご参考)前期連結実績
(2026 年 3 月期) 5,122 685 670 801 418 円 98 銭
(注)前期連結実績は、会計方針の変更に伴い遡及修正が行われたため、遡及修正後の前期連結数値として記載しております。
(東証スタンダード・コード 9127)"""


def pdf(reference=False, text=None):
    n = "20260804508778" if reference else "20260804508762"
    b = PdfStatement(
        ticker="9127",
        disclosure_number=n,
        source_url=f"https://www2.jpx.co.jp/disc/91270/1401{n}.pdf",
        source_sha256=("a" if reference else "b") * 64,
        bundle_id="synthetic",
        resource_name="pdf:" + n,
        received_at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    return extract_pages(b, [text if text is not None else REFERENCE if reference else CURRENT])


def docs():
    return [
        JQuantsFinancialSummary(
            ticker="9127",
            disclosed_at=date(2026, 8, 5),
            disclosed_time="14:45:00",
            disclosure_number="20260804508762",
            document_type="1QFinancialStatements_Consolidated_JP",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            fiscal_year_end=date(2027, 3, 31),
            fiscal_period="1Q",
            sales=1473_000_000,
            operating_profit=338_000_000,
            profit=247_000_000,
            shares_outstanding=1932000,
            treasury_shares=402030,
        ),
        JQuantsFinancialSummary(
            ticker="9127",
            disclosed_at=date(2026, 5, 14),
            disclosed_time="15:00:00",
            disclosure_number="20260514532523",
            document_type="FYFinancialStatements_Consolidated_JP",
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
            fiscal_year_end=date(2026, 3, 31),
            fiscal_period="FY",
            sales=5122_000_000,
            operating_profit=657_000_000,
            profit=774_000_000,
        ),
    ]


def test_actual_reference_not_forecast_or_old_annual():
    c, a = pdf(), pdf(True)
    assert c.reason is a.reason is None
    assert c.pairs[2].comparative == -61
    assert c.pairs[2].current_column is None
    assert c.pairs[2].headline_crosscheck_page is None
    assert a.annual_reference.amounts["parent_profit"] == 801
    assert a.rounding == "unspecified"
    assert PdfStatement.model_validate_json(a.model_dump_json()) == a
    selected = select_pdf_financials(docs(), [a, c], as_of=ASOF)
    assert selected.status == "available"
    assert selected.amounts_yen == {
        "sales": 5591_000_000,
        "operating_profit": 1059_000_000,
        "parent_profit": 1109_000_000,
    }
    assert selected.precision == "derived_from_display_amounts_rounding_unspecified"
    assert selected.annual_revision_basis == "same_day_restated_reference_actual"
    assert select_pdf_financials(docs()[::-1], [c, a, a], as_of=ASOF) == selected
    assert not build_pdf_supplement([a, c], as_of=ASOF).ttm


@pytest.mark.parametrize(
    ("reference", "old", "new", "reason"),
    [
        (False, "遡及後の数値に変更", "原数値を使用", "pdf_comparative_basis_not_explicit"),
        (False, "百万円未満切捨て", "百万円", "pdf_precision_unknown"),
        (False, "コード番号 9127", "コード番号 9999", "pdf_issuer_mismatch"),
        (
            False,
            "2026年4月1日～2026年6月30日",
            "2026年4月1日～2026年9月30日",
            "pdf_headline_period_conflict",
        ),
        (True, "前期連結実績", "前期連結予想", "pdf_annual_reference_missing_or_ambiguous"),
        (
            True,
            "遡及修正後の前期連結数値",
            "変更前の前期連結数値",
            "pdf_comparative_basis_not_explicit",
        ),
        (True, "単位:百万円", "単位:千円", "pdf_statement_unit_unknown"),
        (True, "コード 9127", "コード 9999", "pdf_issuer_mismatch"),
    ],
)
def test_missing_or_unverified_sources_are_not_adopted(reference, old, new, reason):
    x = pdf(reference, (REFERENCE if reference else CURRENT).replace(old, new))
    assert x.reason == reason
    assert not x.pairs
    assert x.annual_reference is None


@pytest.mark.parametrize("change", ["missing", "ambiguous", "wrong_year", "different_day"])
def test_no_old_annual_fallback_for_restated_quarter(change):
    a = pdf(True)
    references = [a]
    if change == "missing":
        references = []
    if change == "ambiguous":
        references.append(a.model_copy(update={"source_sha256": "c" * 64}))
    if change == "wrong_year":
        references = [
            a.model_copy(
                update={
                    "annual_reference": a.annual_reference.model_copy(
                        update={"fiscal_year_end": date(2025, 3, 31)}
                    )
                }
            )
        ]
    if change == "different_day":
        references = [a.model_copy(update={"published_on": date(2026, 8, 6)})]
    value = select_pdf_financials(docs(), [pdf(), *references], as_of=ASOF)
    assert value.status == "unavailable"
    assert not value.amounts_yen
    assert value.reason == "restated_annual_reference_missing_or_ambiguous"


def test_source_revision_and_cutoff_are_not_bypassed():
    assert select_pdf_financials(docs(), [pdf(), pdf(True)], as_of=date(2026, 8, 4)) is None
    corrected = replace(docs()[0], disclosure_number="corrected", disclosed_at=date(2026, 8, 6))
    selected = select_pdf_financials([*docs(), corrected], [pdf(), pdf(True)], as_of=ASOF)
    assert selected.status == "unavailable"
    assert selected.blocks_legacy_ttm


def test_finance_growth_and_operands_share_the_same_display_basis():
    financial, growth, *_ = generate_financial_and_price_models(
        ticker="9127",
        documents=docs(),
        pdf_statements=[pdf(), pdf(True)],
        adjusted_bars=[PriceBar(date(2026, 11, 2), 3475, None)],
        adjustment_events=[],
        trading_dates=[date(2026, 11, 2)],
        expected_price_date=date(2026, 11, 2),
        sector="海運業",
        as_of=ASOF,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )
    selection = financial.payload["financial"]["pdf_selection"]
    assert selection == growth.payload["pdf_selection"]
    e = financial.payload["earnings_price"]
    assert e["profit_ttm_yen"] == 1109_000_000
    assert (
        sum(c["amount_yen"] * c["coefficient"] for c in e["profit_components"])
        == e["profit_ttm_yen"]
    )
    assert {c["document_type"] for c in e["profit_components"]} == {
        "PDFDisplayAnnualReference_Consolidated_JP",
        "PDFDisplayFinancialStatements_Consolidated_JP",
    }
    assert "rounding_unspecified" in str(growth.payload)
