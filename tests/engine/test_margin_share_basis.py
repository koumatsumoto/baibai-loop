"""Normalize observed share units instead of rejecting known corporate actions."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.market_context.history import avg_daily_volume
from baibai_engine.machine_analysis.models.market_context.margin import MarginBalance, margin_supply_demand
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar


@pytest.mark.parametrize(
    ("kind", "factor", "delta"),
    [("1", 0.5, 0.0), ("2", 2.0, 0.075), ("3", 0.8, 0.05), (None, 0.8, None)],
)
def test_margin_comparison_uses_actual_balance_dates_and_known_units(kind, factor, delta):
    prior, action, end = date(2024, 1, 1), date(2025, 1, 2), date(2026, 1, 3)
    # The action is older than 130 sessions, but it still changes the prior balance basis.
    result = margin_supply_demand(
        latest=MarginBalance(balance_date=end, issue_type="2", long_vol=1000.0, short_vol=200.0),
        prior_26w=MarginBalance(balance_date=prior, long_vol=500.0),
        avg_daily_volume_shares=100.0,
        shares_outstanding=10000.0,
        asof_date=end,
        adjustment_events=[JQuantsAdjustmentFactorEvent("1111", action, factor, kind)],
    )
    assert result.margin_long_delta_26w == delta
    # An uncertain old event does not erase a comparison between two current quantities.
    assert result.margin_long_to_adv == 10.0
    assert result.margin_short_to_adv == 2.0


@pytest.mark.parametrize("kind", ["1", None])
def test_balance_after_report_and_before_asof_is_converted_or_left_unknown(kind):
    balance, action, end = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    result = margin_supply_demand(
        latest=MarginBalance(
            balance_date=balance, issue_type="2", long_vol=1000.0, short_vol=250.0
        ),
        prior_26w=None,
        avg_daily_volume_shares=200.0,
        shares_outstanding=10000.0,
        asof_date=end,
        adjustment_events=[JQuantsAdjustmentFactorEvent("1111", action, 0.5, kind)],
    )
    assert result.margin_long_to_adv == (10.0 if kind == "1" else None)
    assert result.margin_short_to_adv == (2.5 if kind == "1" else None)
    assert result.margin_long_share == 0.8


@pytest.mark.parametrize(("kind", "factor"), [("1", 0.5), ("3", 0.8), (None, 0.8)])
def test_volume_mean_uses_the_same_asof_share_unit(kind, factor):
    start = date(2026, 1, 1)
    action = start + timedelta(days=10)
    end = start + timedelta(days=19)
    bars = [
        JQuantsDailyBar(
            "1111",
            start + timedelta(days=i),
            100.0,
            None,
            volume=50.0 if kind == "1" and i < 10 else 100.0,
            adjustment_factor=1.0,
        )
        for i in range(20)
    ]
    events = [JQuantsAdjustmentFactorEvent("1111", action, factor, kind)]
    assert avg_daily_volume(bars, end, adjustment_events=events) == (
        None if kind is None else 100.0
    )


def test_market_context_calculation_revision_changes_run_and_panel_identity(monkeypatch):
    from baibai_engine.machine_analysis.models.market_context import margin
    from baibai_engine.machine_analysis.historical_evaluation import panel, store
    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import load_screening_rules
    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rules_identity import (
        production_rules_contract_hash,
    )

    rules = load_screening_rules()
    run_before = production_rules_contract_hash(rules.model_dump_json())
    panel_before = panel.rules_content_hash(rules)
    cache_before = store._derive_cache_schema_version()
    monkeypatch.setattr(margin, "MARKET_CONTEXT_CALCULATION_REVISION", "different-units")
    monkeypatch.setattr(panel, "MARKET_CONTEXT_CALCULATION_REVISION", "different-units")
    monkeypatch.setattr(store, "MARKET_CONTEXT_CALCULATION_REVISION", "different-units")
    assert production_rules_contract_hash(rules.model_dump_json()) != run_before
    assert panel.rules_content_hash(rules) != panel_before
    assert store._derive_cache_schema_version() != cache_before


def test_observed_zero_balance_needs_no_share_conversion_but_missing_stays_missing():
    before, action, end = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    for quantity in (0.0, None):
        result = margin_supply_demand(
            latest=MarginBalance(balance_date=before, issue_type="2", long_vol=quantity),
            prior_26w=None,
            avg_daily_volume_shares=100.0,
            shares_outstanding=10000.0,
            asof_date=end,
            adjustment_events=[JQuantsAdjustmentFactorEvent("1111", action, 0.8, None)],
        )
        assert result.margin_long_to_adv == quantity


def test_screening_assembles_margin_and_volume_on_one_basis():
    from tests.engine.screening.metric_fixtures import _security, _summary

    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.metric_builder import build_metrics

    start, action, end = date(2026, 1, 1), date(2026, 1, 20), date(2026, 1, 21)
    # Publish the share count after the action, not on an ambiguous disclosure boundary.
    bars = [
        JQuantsDailyBar(
            "1111",
            start + timedelta(days=i),
            50.0 if i == 19 else 100.0,
            10000.0,
            volume=200.0 if i == 19 else 100.0,
            adjustment_factor=1.0,
        )
        for i in range(20)
    ]
    result = build_metrics(
        end,
        {"1111": _security("1111")},
        {"1111": bars},
        {"1111": [_summary("1111", end, shares_outstanding=20000.0)]},
        {},
        margin_latest={
            "1111": MarginBalance(
                balance_date=date(2026, 1, 19), issue_type="2", long_vol=1000.0, short_vol=200.0
            )
        },
        margin_prior_26w={"1111": MarginBalance(balance_date=date(2025, 7, 1), long_vol=1500.0)},
        adjustment_events_by_ticker={
            "1111": [JQuantsAdjustmentFactorEvent("1111", action, 0.5, "1")]
        },
    )
    axes = result.derived["1111"]
    assert axes.margin_long_to_adv == 10.0
    assert axes.margin_short_to_adv == 2.0
    assert axes.margin_long_delta_26w == -0.05
