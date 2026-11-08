"""財務・成長の両ownerで、週次決算暦と実期間の連続性を揃える。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _latest_actual_row,
    _latest_actual_row_by_field_priority,
    _latest_non_null_row,
    _prior_year_summary,
    _ttm_period,
    _ttm_value,
)
from baibai_engine.machine_analysis.models.company_growth.calculations import build_period_states
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality


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
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (120.0, TTMQuality.EXACT)
    period = _ttm_period(rows, "profit", DEFAULT_TTM_RULES)
    assert period is not None
    assert period["start"] == "2023-05-21"
    assert period["end"] == "2024-05-18"
    assert [item["amount_yen"] for item in period["components"]] == [40.0, 100.0, 20.0]
    annual = growth_annual(rows)
    assert annual.period_status == "available"
    assert (annual.period_start, annual.period_end) == (date(2023, 5, 21), date(2024, 5, 18))
    assert annual.metrics["parent_profit"].value == 120.0
    assert len(annual.metrics["parent_profit"].documents) == 3


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


def test_latest_partial_standard_revision_blocks_current_profit_yoy_and_priority():
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
    assert (
        _latest_actual_row_by_field_priority(
            rows, ("operating_profit", "ordinary_profit", "profit")
        )
        is None
    )


def test_fy_cfo_yoy_survives_a_newer_quarter_without_cfo():
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
    assert financial.cfo_yoy == 1.0


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
    assert financial.cfo_yoy == (1.0 if revision_standard == "JP" else None)


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
    assert (
        _latest_actual_row_by_field_priority(
            [annual, tied], ("operating_profit", "ordinary_profit", "profit")
        )
        is None
    )


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


def test_accruals_use_current_assets_when_prior_assets_have_another_standard():
    prior = replace(
        document(date(2023, 4, 1), date(2024, 3, 31), 50.0),
        total_assets=200.0,
    )
    current = replace(
        document(date(2024, 4, 1), date(2025, 3, 31), 40.0),
        document_type="FYFinancialStatements_Consolidated_IFRS",
        total_assets=100.0,
        cfo=10.0,
    )

    def accruals(rows):
        return build_financial_snapshot(
            10.0,
            rows,
            None,
            DEFAULT_TTM_RULES,
            adjustment_events=(),
            asof_date=date(2025, 6, 1),
        ).accruals_to_assets

    assert accruals([prior, current]) == pytest.approx(0.3)
    assert accruals(
        [replace(prior, document_type="FYFinancialStatements_Consolidated_IFRS"), current]
    ) == pytest.approx(0.2)


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
    "revision_type",
    ["FYFinancialStatements_Consolidated_JP", "EarnForecastRevision", "DividendForecastRevision"],
)
def test_same_basis_partial_revision_and_notices_keep_balance_carry(revision_type):
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
    assert result.bs_carry_forward_fields == "bps"
    assert result.bs_carry_forward_lag_days == 1


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
