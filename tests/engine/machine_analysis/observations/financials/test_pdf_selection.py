"""最新の開示選択、同一基準の組替、価格/成長への接続を合成資料で検証する。"""

from dataclasses import replace
from datetime import date
from itertools import permutations

import pytest
from tests.helpers.pdf_statements import ASOF, base, pages, statement

from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    select_pdf_financials,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import extract_pages
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import _change


def document(annual=False):
    pdf = statement(annual)
    amounts = {pair.metric: pair.current * 1_000_000 + 123_000 for pair in pdf.pairs}
    return JQuantsFinancialSummary(
        ticker="9268",
        disclosed_at=pdf.published_on,
        disclosed_time="15:00:00",
        disclosure_number=pdf.disclosure_number,
        document_type=("FY" if annual else "1Q") + "FinancialStatements_Consolidated_IFRS",
        period_start=pdf.current_start,
        period_end=pdf.current_end,
        fiscal_year_end=date(2026 if annual else 2027, 3, 31),
        fiscal_period="FY" if annual else "1Q",
        sales=amounts["sales"],
        operating_profit=amounts["operating_profit"],
        profit=amounts["parent_profit"],
        shares_outstanding=100_000_000,
        treasury_shares=1_000_000,
    )


def models(documents=None, pdfs=None, sector="卸売業"):
    return generate_financial_and_price_models(
        ticker="9268",
        documents=[document(True), document()] if documents is None else documents,
        pdf_statements=[statement(True), statement()] if pdfs is None else pdfs,
        adjusted_bars=[PriceBar(ASOF, 1000, None)],
        adjustment_events=[],
        trading_dates=[ASOF],
        expected_price_date=ASOF,
        sector=sector,
        as_of=ASOF,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )


def test_pdf_selection_is_order_independent_and_never_combines_eps():
    docs, pdfs = [document(), document(True)], [statement(), statement(True)]
    expected = select_pdf_financials(docs, pdfs, as_of=ASOF)
    assert expected.status == "available"
    assert expected.amounts_yen == {
        "sales": 353_037_000_000,
        "operating_profit": 11_386_000_000,
        "parent_profit": 3_027_000_000,
    }
    assert (expected.period_start, expected.period_end) == (date(2025, 7, 1), date(2026, 6, 30))
    for a in permutations(docs):
        for b in permutations(pdfs):
            assert select_pdf_financials(a, b, as_of=ASOF) == expected
    assert select_pdf_financials(docs, [*pdfs, pdfs[0]], as_of=ASOF) == expected


def test_latest_period_is_selected_before_pdf_availability():
    later = replace(
        document(),
        disclosure_number="new-quarter",
        disclosed_at=date(2026, 10, 20),
        fiscal_period="2Q",
        period_end=date(2026, 9, 30),
    )
    value = select_pdf_financials(
        [document(), document(True), later], [statement(True), statement()], as_of=ASOF
    )
    assert value.status == "unavailable"
    assert value.current is None
    assert not value.blocks_legacy_ttm
    assert not value.amounts_yen


def test_missing_correction_does_not_revive_old_pdf_or_legacy_ttm():
    corrected = replace(document(), disclosure_number="corrected", disclosed_at=date(2026, 9, 1))
    result = models([document(True), document(), corrected])
    financial = result[0].payload
    assert financial["financial"]["pdf_selection"]["blocks_legacy_ttm"]
    assert financial["earnings_price"]["profit_ttm_yen"] is None
    assert financial["financial"]["eps"] is None
    reader = CompanyGrowthReader(result[1].payload)
    assert (
        metric_value(reader.observation(reader.latest_period, "annual_ttm"), "parent_profit")
        is None
    )


def test_full_corrected_statement_selected_only_by_its_latest_native_identity():
    corrected = statement().model_copy(
        update={
            "disclosure_number": "corrected",
            "source_sha256": "c" * 64,
            "published_on": date(2026, 9, 1),
        }
    )
    row = replace(document(), disclosure_number="corrected", disclosed_at=date(2026, 9, 1))
    value = select_pdf_financials(
        [document(True), document(), row], [statement(True), statement(), corrected], as_of=ASOF
    )
    assert value.status == "available"
    assert value.current.source_sha256 == "c" * 64
    texts = pages()
    texts[0] = texts[0].replace("2027年3月期 第1", "訂正 2027年3月期 第1")
    assert extract_pages(base(), texts).reason is None


@pytest.mark.parametrize(
    "change",
    [
        {"disclosed_time": None, "disclosure_number": "other"},
        {"profit": 100},
        {"document_type": "1QFinancialStatements_NonConsolidated_IFRS"},
    ],
)
def test_same_day_conflicts_do_not_choose_by_input_order(change):
    other = replace(document(), **change)
    for docs in ([document(True), document(), other], [other, document(), document(True)]):
        result = select_pdf_financials(docs, [statement(True), statement()], as_of=ASOF)
        assert result.status == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"profit": 925_000_000},
        {"unresolved_actual_fields": ("profit",)},
        {"document_type": "1QFinancialStatements_Consolidated_JP"},
        {"period_end": date(2026, 7, 31)},
    ],
)
def test_native_identity_amount_and_basis_conflicts_are_explicit(change):
    result = select_pdf_financials(
        [document(True), replace(document(), **change)], [statement(True), statement()], as_of=ASOF
    )
    assert result.status == "unavailable"
    assert not result.amounts_yen


def test_unordered_pdf_bytes_do_not_use_last_received_as_correction_order():
    changed = statement().model_copy(update={"source_sha256": "c" * 64})
    result = select_pdf_financials(
        [document(True), document()], [statement(True), statement(), changed], as_of=ASOF
    )
    assert result.reason == "latest_disclosure_pdf_missing_or_ambiguous"


def test_future_correction_ignored_and_annual_correction_does_not_mix_vintages():
    future = replace(document(), disclosure_number="future", disclosed_at=date(2026, 11, 2))
    assert (
        select_pdf_financials(
            [document(True), document(), future], [statement(True), statement()], as_of=ASOF
        ).status
        == "available"
    )
    row = replace(
        document(True), disclosure_number="annual-correction", disclosed_at=date(2026, 9, 1)
    )
    pdf = statement(True).model_copy(
        update={"disclosure_number": row.disclosure_number, "published_on": row.disclosed_at}
    )
    result = select_pdf_financials(
        [document(), document(True), row], [statement(), statement(True), pdf], as_of=ASOF
    )
    assert result.reason == "annual_revised_after_current_disclosure"


def annual_correction_on_quarter_day(*, time="16:00:00"):
    row = replace(
        document(True),
        disclosure_number="annual-profit-correction",
        disclosed_at=document().disclosed_at,
        disclosed_time=time,
        profit=2_489_123_000,
    )
    pdf = statement(True).model_copy(
        update={
            "disclosure_number": row.disclosure_number,
            "published_on": row.disclosed_at,
            "source_sha256": "c" * 64,
            "pairs": tuple(
                pair.model_copy(update={"current": 2489, "current_text": "2,489"})
                if pair.metric == "parent_profit"
                else pair
                for pair in statement(True).pairs
            ),
        }
    )
    return row, pdf


@pytest.mark.parametrize(
    ("annual_time", "current_time", "reason"),
    [
        ("14:00:00", "15:00:00", None),
        ("15:00:00", "15:00:00", None),
        ("16:00:00", "15:00:00", "annual_revised_after_current_disclosure"),
        (None, "15:00:00", "annual_disclosure_order_unresolved"),
        ("14:00:00", None, "annual_disclosure_order_unresolved"),
        (None, None, "annual_disclosure_order_unresolved"),
    ],
)
def test_ifrs_same_day_annual_operand_uses_its_own_native_clock(annual_time, current_time, reason):
    row, pdf = annual_correction_on_quarter_day(time=annual_time)
    quarter = replace(document(), disclosed_time=current_time)
    for docs in permutations([document(True), quarter, row]):
        result = select_pdf_financials(docs, [statement(True), statement(), pdf], as_of=ASOF)
        assert result.reason == reason
        assert result.status == ("available" if reason is None else "unavailable")
        assert result.amounts_yen == (
            {}
            if reason
            else {
                "sales": 353_037_000_000,
                "operating_profit": 11_386_000_000,
                "parent_profit": 3_047_000_000,
            }
        )


def test_ifrs_same_day_unchanged_operand_does_not_borrow_later_eps_revision_clock():
    earlier = replace(
        document(True), disclosed_at=document().disclosed_at, disclosed_time="14:00:00"
    )
    pdf = statement(True).model_copy(update={"published_on": earlier.disclosed_at})
    later = replace(
        earlier,
        disclosure_number="eps-only-correction",
        disclosed_time="16:00:00",
        cumulative_eps=36.1,
    )
    result = select_pdf_financials([earlier, document(), later], [pdf, statement()], as_of=ASOF)
    assert result.status == "available"
    assert result.annual_revision_basis == "latest_reconfirmed_unchanged_amounts"
    assert result.amounts_yen["parent_profit"] == 3_027_000_000


def test_ifrs_earlier_day_operand_needs_no_invented_intraday_clock():
    annual = replace(document(True), disclosed_time=None)
    quarter = replace(document(), disclosed_time=None)
    result = select_pdf_financials([annual, quarter], [statement(True), statement()], as_of=ASOF)
    assert result.status == "available"


def test_later_ifrs_annual_operand_is_not_promoted_through_shared_model_generation():
    row, pdf = annual_correction_on_quarter_day()
    financial, growth, _ = models(
        documents=[document(True), document(), row], pdfs=[statement(True), statement(), pdf]
    )
    assert financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert financial.payload["financial"]["sales_ttm"] is None
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    assert metric_value(annual, "parent_profit") is None
    quarter = reader.observation(reader.latest_period, "quarter")
    assert metric_value(quarter, "parent_profit") == 924_000_000


def test_models_share_selected_ttm_and_restated_quarter_comparison():
    financial, growth, _ = models()
    result = financial.payload
    selection = result["financial"]["pdf_selection"]
    assert selection == growth.payload["pdf_selection"]
    assert result["earnings_price"]["profit_ttm_yen"] == 3_027_000_000
    assert result["earnings_price"]["earnings_yield"] == pytest.approx(
        3_027_000_000 / 99_000_000_000
    )
    assert result["financial"]["eps"] == pytest.approx(3_027_000_000 / 99_000_000)
    assert result["financial"]["per_trailing"] == pytest.approx(1000 / result["financial"]["eps"])
    assert result["financial"]["ttm_quality_per_trailing"] == "approximated"
    assert result["financial"]["sales_ttm"] == 353_037_000_000
    assert result["financial"]["operating_profit_ttm"] == 11_386_000_000
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    assert metric_value(annual, "parent_profit", disclosed_by=ASOF) == 3_027_000_000
    quarter = reader.observation(reader.latest_period, "quarter")
    change = _change(reader, quarter, "operating_profit", disclosed_by=ASOF)
    assert change["from_value"] == 1_979_000_000
    assert change["difference"] == 1_549_000_000
    assert change["source_sha256"] == statement().source_sha256
    assert (
        _change(reader, quarter, "operating_profit", disclosed_by=date(2026, 8, 6))["difference"]
        is None
    )


@pytest.mark.parametrize("sector", [None, "銀行業"])
def test_pdf_does_not_remove_sector_specific_margin_limit(sector):
    reader = CompanyGrowthReader(models(sector=sector)[1].payload)
    assert (
        metric_value(reader.observation(reader.latest_period, "quarter"), "operating_margin")
        is None
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "annual_ttm"), "operating_margin")
        is None
    )


def test_annual_direct_selection_and_no_pdf_unchanged():
    value = select_pdf_financials([document(True)], [statement(True)], as_of=ASOF)
    assert value.status == "available"
    assert value.annual is None
    assert value.amounts_yen["parent_profit"] == 2_469_000_000
    assert select_pdf_financials([document()], [], as_of=ASOF) is None
    assert "pdf_selection" not in models(pdfs=[])[0].payload["financial"]


@pytest.mark.parametrize("revision_date", [date(2026, 6, 19), date(2026, 9, 1)])
def test_annual_eps_only_revision_keeps_verified_unchanged_amount_operand(revision_date):
    revised = replace(
        document(True),
        disclosure_number="annual-eps-correction",
        disclosed_at=revision_date,
        cumulative_eps=36.1,
    )
    result = select_pdf_financials(
        [document(True), revised, document()], [statement(True), statement()], as_of=ASOF
    )
    assert result.status == "available"
    assert result.annual_latest_disclosure_number == "annual-eps-correction"
    assert result.annual_revision_basis == "latest_reconfirmed_unchanged_amounts"
    assert result.annual.disclosure_number == statement(True).disclosure_number
    changed = replace(revised, profit=revised.profit + 1)
    rejected = select_pdf_financials(
        [document(True), changed, document()], [statement(True), statement()], as_of=ASOF
    )
    assert rejected.status == "unavailable"


def test_negative_display_uses_truncation_toward_zero():
    pdf = statement().model_copy(
        update={
            "pairs": tuple(
                pair.model_copy(update={"current": -924})
                if pair.metric == "parent_profit"
                else pair
                for pair in statement().pairs
            )
        }
    )
    row = replace(document(), profit=-924_123_000)
    selected = select_pdf_financials([document(True), row], [statement(True), pdf], as_of=ASOF)
    assert selected.status == "available"
    assert selected.amounts_yen["parent_profit"] == 1_179_000_000


def test_annual_current_eps_only_correction_does_not_hide_unchanged_profit():
    row = replace(
        document(True), disclosure_number="annual-correction", disclosed_at=date(2026, 6, 19)
    )
    result = select_pdf_financials([document(True), row], [statement(True)], as_of=ASOF)
    assert result.status == "available"
    assert result.annual_latest_disclosure_number == row.disclosure_number
    assert result.annual_revision_basis == "latest_reconfirmed_unchanged_amounts"
    assert result.amounts_yen["parent_profit"] == 2_469_000_000


def test_unchanged_current_annual_does_not_reconfirm_its_comparative_column():
    row = replace(
        document(True), disclosure_number="annual-correction", disclosed_at=date(2026, 6, 19)
    )
    financial, growth, _ = models(documents=[document(True), row], pdfs=[statement(True)])
    selected = financial.payload["financial"]["pdf_selection"]
    assert selected["status"] == "available"
    assert selected["comparative_verified"] is False
    assert all("source_sha256" not in change for change in growth.payload["changes"])


def test_annual_correction_can_use_native_annual_without_restated_pdf():
    changed = replace(
        document(True),
        disclosure_number="annual-profit-correction",
        disclosed_at=date(2026, 6, 19),
        profit=2_500_000_000,
    )
    financial, _, _ = models(documents=[document(True), changed], pdfs=[statement(True)])
    selected = financial.payload["financial"]["pdf_selection"]
    assert selected["status"] == "unavailable"
    assert selected["blocks_legacy_ttm"] is False
    assert financial.payload["earnings_price"]["profit_ttm_yen"] == 2_500_000_000
