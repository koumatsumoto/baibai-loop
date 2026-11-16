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
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    PdfFinancialSelection,
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


def reference_document(disclosed_time):
    return JQuantsFinancialSummary(
        ticker="9127",
        disclosed_at=date(2026, 8, 5),
        disclosed_time=disclosed_time,
        disclosure_number=pdf(True).disclosure_number,
        document_type="EarnForecastRevision",
    )


def later_interim_docs():
    current, annual = docs()
    return [
        current,
        annual,
        replace(
            current,
            disclosed_at=date(2026, 11, 4),
            disclosure_number="later-interim",
            document_type="2QFinancialStatements_Consolidated_JP",
            period_end=date(2026, 9, 30),
            fiscal_period="2Q",
            sales=2869_000_000,
            operating_profit=654_000_000,
            profit=440_000_000,
        ),
        replace(
            annual,
            disclosed_at=date(2025, 11, 6),
            disclosure_number="prior-interim",
            document_type="2QFinancialStatements_Consolidated_JP",
            period_end=date(2025, 9, 30),
            fiscal_period="2Q",
            sales=2473_000_000,
            operating_profit=54_000_000,
            profit=3_000_000,
        ),
    ]


def test_later_missing_pdf_does_not_forget_known_restated_annual():
    selected = select_pdf_financials(
        later_interim_docs(), [pdf(), pdf(True)], as_of=date(2026, 11, 14)
    )
    assert selected.blocks_legacy_ttm
    assert selected.reason == "known_restated_annual_operand_conflict"
    assert selected.current is None
    assert selected.annual == pdf(True)
    assert not selected.amounts_yen


@pytest.mark.parametrize(
    "case",
    [
        "no_reference",
        "invalid_reference",
        "other_year",
        "other_basis",
        "future_reference",
        "short_annual",
        "annual_gap",
        "ifrs",
        "current_annual",
        "new_annual",
        "same_amounts",
    ],
)
def test_pdf_absence_is_not_itself_a_restatement_conflict(case):
    rows = later_interim_docs()
    reference = pdf(True)
    if case == "invalid_reference":
        reference = reference.model_copy(update={"reason": "pdf_comparative_basis_not_explicit"})
    elif case == "other_year":
        reference = reference.model_copy(
            update={
                "annual_reference": reference.annual_reference.model_copy(
                    update={"fiscal_year_end": date(2025, 3, 31)}
                )
            }
        )
    elif case == "other_basis":
        reference = reference.model_copy(update={"statement_basis": None})
    elif case == "future_reference":
        reference = reference.model_copy(update={"published_on": date(2026, 11, 15)})
    elif case == "short_annual":
        rows[1] = replace(rows[1], period_start=date(2025, 5, 1))
    elif case == "annual_gap":
        rows[1] = replace(rows[1], period_end=date(2026, 3, 30))
    elif case == "ifrs":
        rows = [
            replace(row, document_type=row.document_type.replace("_JP", "_IFRS")) for row in rows
        ]
    elif case == "current_annual":
        rows[2] = replace(
            rows[2],
            period_end=date(2027, 3, 31),
            disclosed_at=date(2027, 5, 14),
            fiscal_period="FY",
        )
    elif case == "new_annual":
        rows.append(replace(rows[1], disclosed_at=date(2026, 9, 1), disclosure_number="new-annual"))
    elif case == "same_amounts":
        rows[1] = replace(rows[1], operating_profit=685_000_000, profit=801_000_000)
    selected = select_pdf_financials(
        rows,
        [pdf()] + ([] if case == "no_reference" else [reference]),
        as_of=date(2027, 5, 15) if case == "current_annual" else date(2026, 11, 14),
    )
    assert not selected.blocks_legacy_ttm


@pytest.mark.parametrize(
    ("annual_time", "reference_time", "blocked"),
    [("14:00:00", "15:00:00", True), ("15:00:00", "14:00:00", False), (None, None, True)],
)
def test_known_annual_conflict_does_not_guess_same_day_order(annual_time, reference_time, blocked):
    rows = later_interim_docs()
    rows[1] = replace(rows[1], disclosed_at=date(2026, 8, 5), disclosed_time=annual_time)
    rows.append(reference_document(reference_time))
    selected = select_pdf_financials(rows, [pdf(), pdf(True)], as_of=date(2026, 11, 14))
    assert selected.blocks_legacy_ttm is blocked


@pytest.mark.parametrize(
    ("amount", "display", "blocked"),
    [
        (774_999_999, 775, False),
        (-774_999_999, -775, False),
        (774_000_000, 775, False),
        (-774_000_000, -775, False),
        (773_000_000, 775, True),
        (-773_000_000, -775, True),
    ],
)
def test_unspecified_display_rounding_is_not_exact_conflict(amount, display, blocked):
    rows = later_interim_docs()
    rows[1] = replace(rows[1], operating_profit=685_000_000, profit=amount)
    reference = pdf(True)
    reference = reference.model_copy(
        update={
            "annual_reference": reference.annual_reference.model_copy(
                update={"amounts": {**reference.annual_reference.amounts, "parent_profit": display}}
            )
        }
    )
    selected = select_pdf_financials(rows, [pdf(), reference], as_of=date(2026, 11, 14))
    assert selected.blocks_legacy_ttm is blocked


def test_later_restated_reference_can_reconfirm_native_annual_amounts():
    reference = pdf(True)
    later = reference.model_copy(
        update={
            "published_on": date(2026, 9, 1),
            "source_sha256": "c" * 64,
            "disclosure_number": "later-reference",
            "annual_reference": reference.annual_reference.model_copy(
                update={"amounts": {"sales": 5122, "operating_profit": 657, "parent_profit": 774}}
            ),
        }
    )
    selected = select_pdf_financials(
        later_interim_docs(), [pdf(), reference, later], as_of=date(2026, 11, 14)
    )
    assert not selected.blocks_legacy_ttm


def test_known_annual_conflict_blocks_only_ttm_and_preserves_observed_periods():
    kwargs = dict(
        ticker="9127",
        documents=later_interim_docs(),
        adjusted_bars=[PriceBar(date(2026, 11, 13), 3295, None)],
        adjustment_events=[],
        trading_dates=[date(2026, 11, 13)],
        expected_price_date=date(2026, 11, 13),
        sector="海運業",
        as_of=date(2026, 11, 14),
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )
    legacy, legacy_growth, *_ = generate_financial_and_price_models(**kwargs)
    financial, growth, *_ = generate_financial_and_price_models(
        **kwargs, pdf_statements=[pdf(), pdf(True)]
    )
    assert legacy.payload["earnings_price"]["profit_ttm_yen"] == 1211_000_000
    assert financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert financial.payload["earnings_price"]["earnings_yield"] is None
    assert financial.payload["earnings_price"]["market_cap_yen"] == 5_041_251_150
    assert financial.payload["financial"]["pdf_selection"] == growth.payload["pdf_selection"]
    assert [c for c in growth.payload["changes"] if c["measurement"] != "annual_ttm"] == [
        c for c in legacy_growth.payload["changes"] if c["measurement"] != "annual_ttm"
    ]
    reader = CompanyGrowthReader(growth.payload)
    assert (
        metric_value(reader.observation(reader.latest_period, "annual_ttm"), "parent_profit")
        is None
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "reported"), "parent_profit")
        == 440_000_000
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "quarter"), "parent_profit")
        == 193_000_000
    )


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


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    ("annual_day", "annual_time", "current_time", "reason"),
    [
        (date(2026, 8, 4), None, "14:45:00", None),
        (date(2026, 8, 5), "14:00:00", "14:45:00", None),
        (date(2026, 8, 5), "14:45:00", "14:45:00", None),
        (
            date(2026, 8, 5),
            "15:00:00",
            "14:45:00",
            "annual_revised_after_current_disclosure",
        ),
        (date(2026, 8, 5), None, "14:45:00", "annual_disclosure_order_unresolved"),
        (date(2026, 8, 5), "14:00:00", None, "annual_disclosure_order_unresolved"),
        (
            date(2026, 9, 1),
            "15:00:00",
            "14:45:00",
            "annual_revised_after_current_disclosure",
        ),
        (date(2026, 11, 4), "15:00:00", "14:45:00", None),
    ],
)
def test_restated_reference_cannot_reconfirm_later_or_unordered_annual_revision(
    changed, annual_day, annual_time, current_time, reason
):
    current, annual = docs()
    current = replace(current, disclosed_time=current_time)
    correction = replace(
        annual,
        disclosed_at=annual_day,
        disclosed_time=annual_time,
        disclosure_number="annual-correction",
        profit=900_000_000 if changed else annual.profit,
    )
    inputs = [current, annual, correction, reference_document("14:45:00")]
    statements = [pdf(), pdf(True)]
    selected = select_pdf_financials(inputs, statements, as_of=ASOF)
    assert selected == select_pdf_financials(inputs[::-1], statements[::-1], as_of=ASOF)
    assert selected.reason == reason
    if reason is None:
        assert selected.status == "available"
        assert selected.amounts_yen["parent_profit"] == 1109_000_000
    else:
        assert selected.status == "unavailable"
        assert not selected.amounts_yen
        assert selected.current == pdf()
        assert selected.annual_latest_disclosure_number == correction.disclosure_number


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    ("reference_time", "reason"),
    [
        ("14:00:00", "annual_revised_after_reference_disclosure"),
        ("14:30:00", None),
        ("14:40:00", None),
        ("15:00:00", None),
        (None, "annual_reference_disclosure_order_unresolved"),
        ("not_received", "annual_reference_disclosure_order_unresolved"),
        ("conflicting", "annual_reference_disclosure_order_unresolved"),
        ("different_disclosure", "annual_reference_disclosure_order_unresolved"),
    ],
)
def test_reference_uses_its_own_publication_clock(changed, reference_time, reason):
    current, annual = docs()
    correction = replace(
        annual,
        disclosed_at=current.disclosed_at,
        disclosed_time="14:30:00",
        disclosure_number="annual-correction",
        profit=900_000_000 if changed else annual.profit,
    )
    inputs = [current, annual, correction]
    if reference_time == "conflicting":
        inputs.extend([reference_document("14:00:00"), reference_document("14:40:00")])
    elif reference_time == "different_disclosure":
        inputs.append(replace(reference_document("14:40:00"), disclosure_number="other"))
    elif reference_time != "not_received":
        inputs.append(reference_document(reference_time))
    statements = [pdf(), pdf(True)]
    selected = select_pdf_financials(inputs, statements, as_of=ASOF)
    assert selected == select_pdf_financials(inputs[::-1], statements[::-1], as_of=ASOF)
    assert selected.reason == reason
    assert selected.status == ("available" if reason is None else "unavailable")
    if reason is None:
        assert selected.amounts_yen["parent_profit"] == 1109_000_000
    else:
        assert not selected.amounts_yen
        assert selected.current == pdf()
        assert selected.annual == pdf(True)
        assert selected.annual_latest_disclosure_number == correction.disclosure_number


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("clock", ["after_current", "after_reference", "unknown_reference"])
def test_annual_revision_masks_both_models_ttm_but_preserves_current_quarter(changed, clock):
    current, annual = docs()
    correction = replace(
        annual,
        disclosed_at=date(2026, 9, 1) if clock == "after_current" else current.disclosed_at,
        disclosed_time="14:30:00",
        disclosure_number="annual-correction",
        profit=900_000_000 if changed else annual.profit,
    )
    inputs = [current, annual, correction]
    if clock == "after_reference":
        inputs.append(reference_document("14:00:00"))
    reason = {
        "after_current": "annual_revised_after_current_disclosure",
        "after_reference": "annual_revised_after_reference_disclosure",
        "unknown_reference": "annual_reference_disclosure_order_unresolved",
    }[clock]
    financial, growth, *_ = generate_financial_and_price_models(
        ticker="9127",
        documents=inputs,
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
    selected = financial.payload["financial"]["pdf_selection"]
    assert selected == growth.payload["pdf_selection"]
    assert selected["reason"] == reason
    assert financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert financial.payload["earnings_price"]["earnings_yield"] is None
    assert financial.payload["earnings_price"]["market_cap_yen"] == 5_316_645_750
    assert financial.payload["financial"]["eps"] is None
    assert financial.payload["financial"]["per_trailing"] is None
    assert financial.payload["financial"]["sales_ttm"] is None
    reader = CompanyGrowthReader(growth.payload)
    assert (
        metric_value(reader.observation(reader.latest_period, "annual_ttm"), "parent_profit")
        is None
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "reported"), "parent_profit")
        == 247_000_000
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "quarter"), "parent_profit")
        == 247_000_000
    )
    assert "v17-pdf-period-basis" in financial.calculation_revision
    assert growth.payload["calculation_method"] == (
        "company_growth_retrospective_v19-pdf-period-basis"
    )


def test_saved_earlier_selection_revision_remains_readable():
    previous = select_pdf_financials(docs(), [pdf(), pdf(True)], as_of=ASOF).model_copy(
        update={"revision": "retained-pdf-restatement-selection-v2"}
    )
    assert PdfFinancialSelection.model_validate_json(previous.model_dump_json()) == previous


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
