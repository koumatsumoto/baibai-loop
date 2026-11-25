"""財務・成長の両ownerで、週次決算暦と実期間の連続性を揃える。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics import periods
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _latest_actual_row,
    _latest_non_null_row,
    _latest_period_operating_profit,
    _prior_year_summary,
    _reported_ttm,
    _ttm_value,
)
from baibai_engine.machine_analysis.models.company_growth.calculations import build_period_states
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar


def document(start, end, amount, *, fiscal_period="FY", fiscal_year_end=None):
    return JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=end + timedelta(days=45),
        fiscal_period=fiscal_period,
        fiscal_year_end=fiscal_year_end or end,
        period_start=start,
        period_end=end,
        sales=amount * 10,
        operating_profit=amount,
        profit=amount,
        disclosure_number=f"week-calendar-{end}",
        document_type="FinancialStatements_Consolidated_JP",
    )


def weekly_documents(prior_start):
    prior_end = date(2024, 2, 17)
    return [
        document(
            prior_start,
            date(2023, 5, 20),
            20.0,
            fiscal_period="1Q",
            fiscal_year_end=prior_end,
        ),
        document(prior_start, prior_end, 100.0),
        document(
            date(2024, 2, 18),
            date(2024, 5, 18),
            40.0,
            fiscal_period="1Q",
            fiscal_year_end=date(2025, 2, 15),
        ),
    ]


def growth_annual(rows):
    state = build_period_states(rows, sector="情報・通信業")[-1]
    return next(item for item in state.observations if item.measurement == "annual_ttm")


@pytest.mark.parametrize("prior_start", [date(2023, 2, 19), date(2023, 2, 12)])
def test_week_calendar_ttm_uses_adjoining_52_or_53_week_fy(prior_start):
    rows = weekly_documents(prior_start)
    selected = _reported_ttm(rows, "profit", DEFAULT_TTM_RULES)
    assert selected.value_and_quality() == (120.0, TTMQuality.EXACT)
    period = selected.period_payload()
    assert period is not None
    assert period["start"] == "2023-05-21"
    assert period["end"] == "2024-05-18"
    assert [item["amount_yen"] for item in period["components"]] == [40.0, 100.0, 20.0]
    annual = growth_annual(rows)
    assert annual.period_status == "available"
    assert (annual.period_start, annual.period_end) == (date(2023, 5, 21), date(2024, 5, 18))
    assert annual.metrics["parent_profit"].value == 120.0
    assert len(annual.metrics["parent_profit"].documents) == 3


@pytest.mark.parametrize("reverse", [False, True])
def test_ttm_subtracts_the_cumulative_of_the_selected_fiscal_year(reverse):
    rows = weekly_documents(date(2023, 2, 19))
    prior, full_year, current = rows
    conflicting_year = replace(
        prior,
        fiscal_year_end=date(2024, 4, 30),
        profit=160.0,
        disclosure_number="different-fiscal-year",
    )
    inputs = [prior, conflicting_year, full_year, current]
    if reverse:
        inputs.reverse()
    selected = _reported_ttm(inputs, "profit", DEFAULT_TTM_RULES)
    assert selected.value_and_quality() == (120.0, TTMQuality.EXACT)
    period = selected.period_payload()
    assert period is not None
    assert period["components"][-1]["disclosure_number"] == prior.disclosure_number
    annual = growth_annual(inputs)
    assert annual.period_status == "available"
    assert annual.metrics["parent_profit"].value == 120.0
    assert "different-fiscal-year" not in {
        source.disclosure_number for source in annual.metrics["parent_profit"].documents
    }


def test_ttm_neither_borrows_a_different_year_nor_revives_a_missing_operand():
    prior, full_year, current = weekly_documents(date(2023, 2, 19))
    conflicting_year = replace(prior, fiscal_year_end=date(2024, 4, 30))
    incompatible = [conflicting_year, full_year, current]
    assert _ttm_value(incompatible, "profit", DEFAULT_TTM_RULES) == (
        None,
        TTMQuality.UNAVAILABLE,
    )
    annual = growth_annual(incompatible)
    assert annual.period_status == "accounting_periods_incomparable"
    assert annual.metrics["parent_profit"].value is None
    assert growth_annual([full_year, current]).period_status == "matching_period_not_observed"
    incomplete_correction = replace(
        prior,
        disclosed_at=prior.disclosed_at + timedelta(days=1),
        disclosure_number="same-fiscal-year-missing-profit",
        profit=None,
    )
    assert _ttm_value(
        [prior, incomplete_correction, conflicting_year, full_year, current],
        "profit",
        DEFAULT_TTM_RULES,
    ) == (None, TTMQuality.UNAVAILABLE)


def test_ttm_retains_compatible_actual_dates_when_comparative_year_is_unrecorded():
    prior, full_year, current = weekly_documents(date(2023, 2, 19))
    prior = replace(prior, fiscal_year_end=None)
    assert _ttm_value([prior, full_year, current], "profit", DEFAULT_TTM_RULES) == (
        120.0,
        TTMQuality.EXACT,
    )
    assert growth_annual([prior, full_year, current]).metrics["parent_profit"].value == 120.0


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", ["unknown_conflicts", "explicit_match", "different_interval"])
def test_ttm_never_promotes_an_unrecorded_year_to_resolve_a_conflicting_interval(case, reverse):
    prior, full_year, current = weekly_documents(date(2023, 2, 19))
    unknown = replace(prior, fiscal_year_end=None, disclosure_number="year-unrecorded")
    other_year = replace(
        prior,
        fiscal_year_end=date(2024, 3, 30),
        period_end=date(2023, 5, 21) if case == "different_interval" else prior.period_end,
        profit=160.0,
        disclosure_number="other-year",
    )
    rows = [unknown, other_year, full_year, current]
    if case == "explicit_match":
        rows.append(replace(prior, profit=35.0))
    if reverse:
        rows.reverse()
    annual = growth_annual(rows)
    if case == "unknown_conflicts":
        assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)
        assert annual.period_status == "accounting_periods_incomparable"
        assert annual.metrics["parent_profit"].reason == "accounting_periods_incomparable"
    else:
        expected = 105.0 if case == "explicit_match" else 120.0
        assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (expected, TTMQuality.EXACT)
        assert annual.period_status == "available"
        assert annual.metrics["parent_profit"].value == expected


@pytest.mark.parametrize(
    ("start", "expected"),
    [(date(2024, 2, 25), 100.0), (date(2024, 2, 18), 100.0), (date(2024, 3, 23), None)],
)
def test_reported_52_and_53_week_years_are_annual_but_eleven_months_are_not(start, expected):
    rows = [document(start, date(2025, 2, 22), 100.0)]
    value, quality = _ttm_value(rows, "profit", DEFAULT_TTM_RULES)
    assert value == expected
    assert quality == (TTMQuality.EXACT if expected is not None else TTMQuality.UNAVAILABLE)
    annual = growth_annual(rows)
    assert annual.metrics["parent_profit"].value == expected
    if expected is not None:
        assert annual.period_status == "available"
        assert annual.period_start == start


@pytest.mark.parametrize("incomparable", ["gap", "basis"])
def test_week_calendar_support_does_not_hide_a_gap_or_mix_statement_bases(incomparable):
    rows = weekly_documents(date(2023, 2, 19))
    if incomparable == "gap":
        # Keep the annual metadata but remove one day from the reported FY interval.
        rows[1] = replace(rows[1], period_end=date(2024, 2, 16))
    else:
        rows[1] = replace(rows[1], document_type="FinancialStatements_NonConsolidated_JP")
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)
    assert growth_annual(rows).metrics["parent_profit"].value is None


def test_jp_to_ifrs_transition_keeps_reported_amount_but_blocks_ttm_and_yoy():
    rows = weekly_documents(date(2023, 2, 19))
    rows[2] = replace(rows[2], document_type="1QFinancialStatements_Consolidated_IFRS")
    assert rows[2].profit == 40.0
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="profit") is None
    assert growth_annual(rows).metrics["parent_profit"].reason == (
        "accounting_standard_incomparable"
    )


def test_same_time_standard_conflict_cannot_pick_an_arbitrary_ttm_component():
    rows = weekly_documents(date(2023, 2, 19))
    rows.append(
        replace(
            rows[2],
            document_type="1QFinancialStatements_Consolidated_IFRS",
            disclosure_number="same-time-ifrs",
        )
    )
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)


def test_share_count_yoy_is_exempt_from_accounting_standard_comparison():
    rows = weekly_documents(date(2023, 2, 19))
    rows[0] = replace(rows[0], shares_outstanding=100.0)
    rows[2] = replace(
        rows[2],
        shares_outstanding=120.0,
        document_type="1QFinancialStatements_Consolidated_IFRS",
    )
    prior = _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="shares_outstanding")
    assert prior is not None
    assert prior.shares_outstanding == 100.0


def test_later_partial_correction_cannot_restore_an_old_basis_profit():
    rows = weekly_documents(date(2023, 2, 19))
    rows.append(
        replace(
            rows[0],
            disclosed_at=date(2023, 7, 15),
            disclosure_number="ifrs-correction",
            document_type="1QFinancialStatements_Consolidated_IFRS",
            profit=None,
        )
    )
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="profit") is None
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)


def test_profit_only_correction_keeps_prior_shares_but_share_correction_wins():
    rows = weekly_documents(date(2023, 2, 19))
    rows[0] = replace(rows[0], shares_outstanding=100.0)
    rows[2] = replace(rows[2], shares_outstanding=120.0)
    correction = replace(
        rows[0],
        disclosed_at=rows[0].disclosed_at + timedelta(days=1),
        disclosure_number="profit-only-ifrs-correction",
        document_type="1QFinancialStatements_Consolidated_IFRS",
        profit=None,
        shares_outstanding=None,
    )
    prior = _prior_year_summary(
        [*rows, correction], DEFAULT_TTM_RULES, field_name="shares_outstanding"
    )
    assert prior is not None
    assert prior.shares_outstanding == 100.0
    changed = replace(
        correction,
        disclosed_at=correction.disclosed_at + timedelta(days=1),
        disclosure_number="shares-correction",
        shares_outstanding=95.0,
    )
    prior = _prior_year_summary(
        [*rows, correction, changed], DEFAULT_TTM_RULES, field_name="shares_outstanding"
    )
    assert prior is not None
    assert prior.shares_outstanding == 95.0


def test_latest_partial_standard_revision_blocks_current_profit_yoy_and_operating_line():
    rows = weekly_documents(date(2023, 2, 19))
    revised = replace(
        rows[2],
        disclosed_at=rows[2].disclosed_at + timedelta(days=1),
        disclosure_number="latest-ifrs-without-profit",
        document_type="1QFinancialStatements_Consolidated_IFRS",
        profit=None,
        operating_profit=None,
        ordinary_profit=None,
    )
    rows.append(revised)
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="profit") is None
    assert _latest_period_operating_profit(rows) is None


def test_fy_cfo_survives_a_newer_quarter_without_cfo():
    prior = replace(
        document(date(2023, 4, 1), date(2024, 3, 31), 50.0),
        cfo=50.0,
    )
    current = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        cfo=100.0,
    )
    quarter = document(
        date(2025, 4, 1),
        date(2025, 6, 30),
        20.0,
        fiscal_period="1Q",
        fiscal_year_end=date(2026, 3, 31),
    )
    rows = [prior, current, quarter]
    assert _latest_actual_row(rows, field_names=("cfo",)) == current
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="cfo") == prior
    financial = build_financial_snapshot(
        10.0,
        rows,
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2025, 9, 1),
    )
    assert financial.cfo == 100.0
    assert financial.latest_financial_disclosure_date == quarter.disclosed_at
    assert financial.cfo_source == {
        "period_type": "FY",
        "period_start": "2024-04-01",
        "period_end": "2025-03-31",
        "statement_basis": "consolidated",
        "accounting_standard": "JP",
        "disclosure_number": current.disclosure_number,
        "disclosed_at": current.disclosed_at.isoformat(),
        "disclosed_time": None,
        "document_type": current.document_type,
    }


@pytest.mark.parametrize(
    ("case", "cfo", "ocf_ttm", "start", "end", "method", "amounts"),
    [
        ("annual", 40.0, 40.0, "2023-02-19", "2024-02-17", "reported_annual", [40.0]),
        (
            "composed",
            15.0,
            50.0,
            "2023-05-21",
            "2024-05-18",
            "fy_plus_current_ytd_minus_prior_ytd",
            [15.0, 40.0, -5.0],
        ),
        ("missing", 40.0, None, None, None, None, []),
    ],
)
def test_generation_resolves_operating_cash_flow_operands_once(
    monkeypatch, case, cfo, ocf_ttm, start, end, method, amounts
):
    selector = periods._ttm_components
    selected = []

    def count_cfo(summaries, field, rules):
        components = selector(summaries, field, rules)
        if field == "cfo":
            selected.append(components)
        return components

    monkeypatch.setattr(periods, "_ttm_components", count_cfo)
    rows = [
        replace(row, cfo=value, total_assets=assets, shares_outstanding=100.0, treasury_shares=0.0)
        for row, value, assets in zip(
            weekly_documents(date(2023, 2, 19)),
            (5.0, 40.0, 15.0),
            (200.0, 300.0, 400.0),
            strict=True,
        )
    ]
    if case == "annual":
        rows = rows[1:2]
    if case == "missing":
        rows[-1] = replace(rows[-1], cfo=None)
    as_of = date(2024, 7, 15)
    financial, _, _ = generate_financial_and_price_models(
        ticker="1001",
        documents=rows,
        adjusted_bars=[PriceBar(as_of, 10.0, None)],
        adjustment_events=(),
        trading_dates=[as_of],
        expected_price_date=as_of,
        sector="情報・通信業",
        as_of=as_of,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )
    result = financial.payload["financial"]
    assert result["cfo"] == cfo
    reported = rows[1] if case == "missing" else rows[-1]
    assert result["cfo_source"]["disclosure_number"] == reported.disclosure_number
    assert result["ocf_ttm"] == ocf_ttm
    assert "accruals_to_assets" not in result
    assert result["ocf_yield"] == (None if ocf_ttm is None else ocf_ttm / 1000.0)
    expected_quality = "unavailable" if ocf_ttm is None else "exact"
    assert result["ttm_quality_pcfr"] == result["ttm_quality_ocf_yield"] == expected_quality
    period = result["ocf_ttm_period"]
    if ocf_ttm is None:
        assert period is None
    else:
        assert (period["start"], period["end"], period["method"]) == (start, end, method)
        components = period["components"]
        assert [part["coefficient"] * part["amount_yen"] for part in components] == amounts
        assert [part["disclosure_number"] for part in components] == [
            row.disclosure_number for row in selected[0]
        ]
    assert len(selected) == 1


@pytest.mark.parametrize("revision_standard", ["JP", "IFRS"])
def test_prior_cfo_empty_correction_preserves_field_only_on_the_same_basis(revision_standard):
    prior = replace(
        document(date(2023, 4, 1), date(2024, 3, 31), 50.0),
        cfo=50.0,
    )
    current = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        cfo=100.0,
    )
    correction = replace(
        prior,
        disclosed_at=prior.disclosed_at + timedelta(days=1),
        disclosure_number="prior-profit-only-correction",
        document_type=f"FYFinancialStatements_Consolidated_{revision_standard}",
        profit=60.0,
        cfo=None,
    )
    rows = [prior, correction, current]
    selected = _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="cfo")
    if revision_standard == "JP":
        assert selected == prior
    else:
        assert selected is None
    financial = build_financial_snapshot(
        10.0,
        rows,
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2025, 6, 1),
    )
    assert financial.cfo == 100.0


def test_conflicted_preferred_operating_line_cannot_fall_through_to_ordinary_profit():
    annual = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        ordinary_profit=50.0,
    )
    tied = replace(
        annual,
        disclosure_number="same-time-operating-conflict",
        operating_profit=120.0,
    )
    assert _latest_period_operating_profit([annual, tied]) is None


def test_same_time_standard_disagreement_preserves_agreed_issuer_shares():
    annual = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        shares_outstanding=100.0,
        treasury_shares=20.0,
    )
    tied = replace(
        annual,
        disclosure_number="same-time-ifrs",
        document_type="FYFinancialStatements_Consolidated_IFRS",
    )
    assert _latest_non_null_row([annual, tied], "shares_outstanding") is not None
    assert _ttm_value([annual, tied], "profit", DEFAULT_TTM_RULES) == (
        None,
        TTMQuality.UNAVAILABLE,
    )
    result = build_financial_snapshot(
        10.0,
        [annual, tied],
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2025, 6, 1),
    )
    assert result.market_cap == 800.0


def test_growth_reports_current_partial_standard_revision_and_keeps_issuer_shares():
    prior, _, current = weekly_documents(date(2023, 2, 19))
    current = replace(current, shares_outstanding=120.0)
    correction = replace(
        current,
        disclosed_at=current.disclosed_at + timedelta(days=1),
        disclosure_number="current-ifrs-profit-omitted",
        document_type="1QFinancialStatements_Consolidated_IFRS",
        profit=None,
        shares_outstanding=None,
    )
    state = build_period_states([prior, current, correction], sector="情報・通信業")[-1]
    reported = next(item for item in state.observations if item.measurement == "reported")
    balance = next(item for item in state.observations if item.measurement == "balance")
    assert reported.metrics["parent_profit"].reason == "accounting_standard_incomparable"
    assert balance.metrics["issued_shares"].value == 120.0


@pytest.mark.parametrize(
    "notice_type",
    ["DividendForecastRevision", "REITDividendForecastRevision", "REITEarnForecastRevision"],
)
def test_same_time_dividend_notice_does_not_revoke_actual_statement_basis(notice_type):
    annual = document(date(2024, 4, 1), date(2025, 3, 31), 100.0)
    dividend = replace(
        annual,
        disclosure_number="dividend-notice",
        document_type=notice_type,
        sales=None,
        operating_profit=None,
        profit=None,
        dps_actual_annual=5.0,
    )
    assert _ttm_value([annual, dividend], "profit", DEFAULT_TTM_RULES) == (
        100.0,
        TTMQuality.EXACT,
    )
    state = build_period_states([annual, dividend], sector="情報・通信業")[-1]
    reported = next(item for item in state.observations if item.measurement == "reported")
    assert reported.metrics["parent_profit"].value == 100.0
    assert reported.accounting_standard == "JP"


@pytest.mark.parametrize(
    "revision_type",
    ["FYFinancialStatements_Consolidated_IFRS", "FYFinancialStatements_NonConsolidated_JP"],
)
@pytest.mark.parametrize("same_day", [False, True])
def test_partial_basis_correction_cannot_revive_old_balance_sheet(revision_type, same_day):
    annual = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        disclosed_time="15:00:00",
        bps=5.0,
        total_assets=1000.0,
        equity_to_asset_ratio=0.5,
        cash_eq=100.0,
        shares_outstanding=100.0,
        treasury_shares=0.0,
    )
    correction = replace(
        annual,
        disclosed_at=annual.disclosed_at + timedelta(days=0 if same_day else 1),
        disclosed_time="16:00:00",
        disclosure_number="partial-basis-correction",
        document_type=revision_type,
        bps=None,
        total_assets=None,
        equity_to_asset_ratio=None,
        cash_eq=None,
        shares_outstanding=None,
        treasury_shares=None,
    )
    rows = [annual, correction]
    for field in ("bps", "total_assets", "equity_to_asset_ratio", "cash_eq"):
        assert _latest_non_null_row(rows, field) is None
    result = build_financial_snapshot(
        10.0, rows, None, DEFAULT_TTM_RULES, adjustment_events=(), asof_date=date(2025, 6, 1)
    )
    assert result.same_state_equity_yen is None
    assert result.total_assets is None
    assert result.cash_eq is None
    assert result.equity_ratio is None
    assert result.pbr is None
    assert result.market_cap == 1000.0
    if not same_day:
        before_correction = build_financial_snapshot(
            10.0,
            rows,
            None,
            DEFAULT_TTM_RULES,
            adjustment_events=(),
            asof_date=annual.disclosed_at,
        )
        assert before_correction.pbr == 2.0
        assert before_correction.cash_eq == 100.0
    assert annual.bps == 5.0
    assert annual.total_assets == 1000.0


@pytest.mark.parametrize(
    ("revision_type", "expected_lag"),
    [
        ("FYFinancialStatements_Consolidated_JP", 1),
        ("EarnForecastRevision", 0),
        ("DividendForecastRevision", 0),
    ],
)
def test_only_a_financial_statement_can_advance_the_balance_carry_clock(
    revision_type, expected_lag
):
    annual = replace(document(date(2024, 4, 1), date(2025, 3, 31), 100.0), bps=5.0)
    correction = replace(
        annual,
        disclosed_at=annual.disclosed_at + timedelta(days=1),
        disclosure_number="same-basis-partial",
        document_type=revision_type,
        bps=None,
    )
    assert _latest_non_null_row([annual, correction], "bps") == annual
    result = build_financial_snapshot(
        10.0,
        [annual, correction],
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2025, 6, 1),
    )
    assert result.latest_financial_disclosure_date == correction.disclosed_at
    assert result.bs_carry_forward_fields == ("bps" if expected_lag else None)
    assert result.bs_carry_forward_lag_days == expected_lag


def test_late_older_period_correction_does_not_change_current_balance_carry():
    annual = replace(document(date(2025, 4, 1), date(2026, 3, 31), 100.0), cash_eq=60.0)
    old = replace(
        document(
            date(2026, 4, 1),
            date(2026, 6, 30),
            20.0,
            fiscal_period="1Q",
            fiscal_year_end=date(2027, 3, 31),
        ),
        shares_outstanding=100.0,
        treasury_shares=0.0,
    )
    current = replace(
        old,
        disclosed_at=date(2026, 11, 11),
        disclosure_number="current-half-year",
        fiscal_period="2Q",
        period_end=date(2026, 9, 30),
        total_assets=1000.0,
        equity_to_asset_ratio=0.5,
    )
    correction = replace(
        old,
        disclosed_at=date(2026, 11, 25),
        disclosure_number="late-first-quarter-correction",
        shares_outstanding=101.0,
    )

    def snapshot(rows, asof):
        return build_financial_snapshot(
            10.0, rows, None, DEFAULT_TTM_RULES, adjustment_events=(), asof_date=asof
        )

    before = snapshot([annual, old, current], correction.disclosed_at)
    after = snapshot([annual, old, current, correction], correction.disclosed_at)
    assert before.bs_carry_forward_fields == "cash_eq"
    assert after.latest_financial_disclosure_date == correction.disclosed_at
    assert replace(after, latest_financial_disclosure_date=current.disclosed_at) == before

    next_report = document(
        current.period_start,
        date(2026, 12, 31),
        30.0,
        fiscal_period="3Q",
        fiscal_year_end=current.fiscal_year_end,
    )
    current_missing = snapshot(
        [annual, old, current, correction, next_report], next_report.disclosed_at
    )
    assert current_missing.total_assets == 1000.0
    assert current_missing.equity_ratio == 0.5
    assert current_missing.cash_eq == 60.0
    assert current_missing.bs_carry_forward_fields == "cash_eq,equity_to_asset_ratio,total_assets"
    assert (
        current_missing.bs_carry_forward_lag_days
        == (next_report.disclosed_at - annual.disclosed_at).days
    )


def test_unknown_latest_actual_balance_clock_does_not_discard_older_observed_values():
    annual = replace(document(date(2024, 4, 1), date(2025, 3, 31), 100.0), cash_eq=60.0)
    current = document(
        date(2025, 4, 1),
        date(2025, 6, 30),
        20.0,
        fiscal_period="1Q",
        fiscal_year_end=date(2026, 3, 31),
    )
    other_basis = replace(
        current,
        disclosure_number="incomparable-current-basis",
        document_type="1QFinancialStatements_Consolidated_IFRS",
    )
    snapshot = build_financial_snapshot(
        10.0,
        [annual, current, other_basis],
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=current.disclosed_at,
    )
    assert snapshot.cash_eq == 60.0
    assert snapshot.bs_carry_forward_fields is None
    assert snapshot.bs_carry_forward_lag_days is None


def test_balance_carry_checks_source_period_not_newer_quarter_basis():
    annual = replace(document(date(2024, 4, 1), date(2025, 3, 31), 100.0), bps=5.0)
    quarter = replace(
        document(
            date(2025, 4, 1),
            date(2025, 6, 30),
            20.0,
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 3, 31),
        ),
        document_type="1QFinancialStatements_Consolidated_IFRS",
    )
    # A new quarter does not withdraw the older period's reported balance sheet.
    assert _latest_non_null_row([annual, quarter], "bps") == annual
    correction = replace(
        annual,
        disclosed_at=quarter.disclosed_at + timedelta(days=1),
        disclosure_number="delayed-annual-basis-correction",
        document_type="FYFinancialStatements_Consolidated_IFRS",
        bps=None,
    )
    assert _latest_non_null_row([annual, quarter, correction], "bps") is None


@pytest.mark.parametrize("correction_time", ["15:00:00", None])
def test_ambiguous_same_day_basis_revision_does_not_revive_bps(correction_time):
    annual = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
        bps=5.0,
        disclosed_time="15:00:00",
    )
    correction = replace(
        annual,
        document_type="FYFinancialStatements_Consolidated_IFRS",
        disclosure_number="ambiguous-basis-correction",
        disclosed_time=correction_time,
        bps=None,
    )
    for rows in ([annual, correction], [correction, annual]):
        assert _latest_non_null_row(rows, "bps") is None
