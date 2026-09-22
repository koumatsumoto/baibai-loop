from dataclasses import replace
from datetime import date

import pytest
from tests.engine.test_review_set import _analysis, _build_review_set
from tests.engine.test_screening_metrics import _daily_bars, _security, _summary

from baibai_engine.screening.calibration.panel import rules_content_hash
from baibai_engine.screening.discovery import review_set as discovery
from baibai_engine.screening.metrics import build_metrics
from baibai_engine.screening.providers.jquants import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.schema import DerivedMetrics
from baibai_engine.screening.security_analysis_builder import build_security_analysis_metrics


def _periods(quarter):
    month, day = {1: (6, 30), 2: (9, 30), 3: (12, 31)}[quarter]
    prior = _summary(
        "130A",
        date(2025, month, day),
        fiscal_period=f"{quarter}Q",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2025, month, day),
        sales=21_464.0 * quarter,
        operating_profit=792.0 * quarter,
    )
    fy = replace(
        prior,
        disclosed_at=date(2026, 5, 1),
        fiscal_period="FY",
        period_end=date(2026, 3, 31),
        sales=105_554.0,
        operating_profit=6_579.0,
    )
    current = replace(
        prior,
        disclosed_at=date(2026, month, day),
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, month, day),
        sales=27_787.0 * quarter,
        operating_profit=2_104.0 * quarter,
    )
    return [prior, fy, current]


def _analysis_from_source(rows):
    asof = date(2027, 1, 15)
    financial = build_metrics(
        asof_date=asof,
        securities_by_ticker={"130A": _security()},
        bars_by_ticker={"130A": _daily_bars("130A", asof, 60)},
        summaries_by_ticker={"130A": rows},
        edinet_by_ticker={},
    ).financials["130A"]
    # Other independent eligibility inputs stay fixed; profitability comes only
    # from the source -> financial -> Security Analysis production owners.
    financial = replace(
        financial,
        total_assets=200_000.0,
        equity_ratio=0.6,
        debt=20_000.0,
        cash=30_000.0,
        fcf_yield=0.08,
        sales_yoy=0.05,
    )
    row = _analysis("130A")
    row["metrics"] = dict(
        build_security_analysis_metrics(
            financial,
            freshness_warning_count=0,
            derived=DerivedMetrics(sector_median_gap={"p_s": -0.4}),
        )
    )
    return financial, row


@pytest.mark.parametrize("quarter", [1, 2, 3, 4])
def test_source_to_analysis_to_review_set_uses_same_ttm_period(quarter):
    rows = _periods(min(quarter, 3))
    if quarter == 4:
        rows = rows[:2]
    financial, row = _analysis_from_source(rows)
    profit = 6_579.0 if quarter == 4 else 6_579.0 + (2_104 - 792) * quarter
    sales = 105_554.0 if quarter == 4 else 105_554.0 + (27_787 - 21_464) * quarter
    assert financial.operating_profit == rows[-1].operating_profit
    assert financial.operating_profit_ttm == profit
    values = discovery._reinvestment_values(row)
    assert values is not None
    assert values.operating_margin == pytest.approx(profit / sales)
    assert values.capital_return == pytest.approx(profit / 110_000)
    entries = _build_review_set([row])["entries"]
    assert any(
        n["valuation_approach_id"] == "reinvestment-value" for n in entries[0]["nominations"]
    )


@pytest.mark.parametrize("missing", ["prior", "latest_revision", "sales_period_mismatch"])
def test_missing_operating_profit_fails_closed_without_other_profit_fallback(missing):
    rows = _periods(1)
    if missing == "prior":
        rows[0] = replace(rows[0], operating_profit=None, ordinary_profit=900.0)
    else:
        # A new actual revision/period with sales must not select older operating profit.
        rows.append(
            replace(
                rows[-1],
                disclosed_at=date(2026, 9, 30),
                operating_profit=None,
                ordinary_profit=9_000.0,
                period_end=date(2026, 9, 30)
                if missing == "sales_period_mismatch"
                else rows[-1].period_end,
            )
        )
    financial, row = _analysis_from_source(rows)
    assert financial.operating_profit_ttm is None
    assert discovery._reinvestment_values(row) is None
    assert not any(
        n["valuation_approach_id"] == "reinvestment-value"
        for e in _build_review_set([row])["entries"]
        for n in e["nominations"]
    )


def test_forecast_only_disclosure_preserves_actual_ttm():
    rows = _periods(1)
    expected, _ = _analysis_from_source(rows)
    rows.append(
        JQuantsFinancialSummary(
            ticker="130A", disclosed_at=date(2026, 10, 1), forecast_profit=10_000.0
        )
    )
    actual, _ = _analysis_from_source(rows)
    assert actual.operating_profit_ttm == expected.operating_profit_ttm == 7_891.0
    assert actual.sales_ttm == expected.sales_ttm == 111_877.0


def test_ttm_changes_quality_eligibility_order_and_union():
    peers = [_analysis(str(1000 + i)) for i in range(25)]
    target = _analysis("9999", per=100, normalized_per=100, asset_ratio=0.01)
    target["metrics"].update(operating_profit=3.0, operating_profit_ttm=24.0)
    old = {**target, "metrics": {**target["metrics"], "operating_profit_ttm": 3.0}}
    before = _build_review_set([*peers, old])
    after = _build_review_set([*peers, target])
    assert "9999" not in {e["ticker"] for e in before["entries"]}
    entry = next(e for e in after["entries"] if e["ticker"] == "9999")
    assert [(n["valuation_approach_id"], n["rank"]) for n in entry["nominations"]] == [
        ("reinvestment-value", 1)
    ]
    floors = discovery._reinvestment_quality_floors([*peers, target])
    assert floors["9999"].operating_margin == pytest.approx(0.12)
    assert floors["9999"].capital_return == pytest.approx(12 / 110)


def test_new_calculation_has_distinct_production_and_calibration_identity(monkeypatch):
    from baibai_engine.screening import metrics
    from baibai_engine.screening.calibration import panel

    rules = load_screening_rules()
    current = production_rules_contract_hash(rules.model_dump_json())
    calibration = rules_content_hash(rules)
    monkeypatch.setattr(
        metrics, "VALUATION_CALCULATION_REVISION", "actual-ttm-latest-accounting-period-v22"
    )
    monkeypatch.setattr(
        panel, "VALUATION_CALCULATION_REVISION", "actual-ttm-latest-accounting-period-v22"
    )
    assert production_rules_contract_hash(rules.model_dump_json()) != current
    assert rules_content_hash(rules) != calibration
