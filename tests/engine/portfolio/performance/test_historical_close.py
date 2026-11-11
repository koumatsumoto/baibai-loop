"""過去の実績も、その営業日の引けまでに生じた時刻付き事実だけを含める。"""

from dataclasses import replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.observations.prices.benchmark_observation import (
    BenchmarkObservation,
)
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.performance.calculate import (
    compute_portfolio_outcome,
    outcome_to_payload,
)
from baibai_engine.portfolio.performance.store import (
    PortfolioOutcomeConflictError,
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)


def benchmark(end="2024-11-01"):
    return BenchmarkObservation.model_validate(
        {
            "schema_version": 1,
            "kind": "benchmark_observation",
            "benchmark_id": "jpx-topix-gross-total-return",
            "index_name": "TOPIX",
            "dividend_treatment": "gross_total_return",
            "horizon": "1y",
            "period_start_date": "2023-11-02",
            "period_end_date": end,
            "period_basis": "official_explicit",
            "period_rule_source_url": None,
            "cumulative_return_pct": 0.0,
            "annualized_return_pct": None,
            "display_precision_bps": 0,
            "source_url": "https://www.jpx.co.jp/synthetic-observation",
            "source_as_of": end,
            "published_at": "2024-11-06",
            "retrieved_at": "2024-11-07T00:00:00+09:00",
        }
    )


def outcome(*, end="2024-11-01", clock="15:10:00", opening_clock="09:00:00", reported=False):
    opening = {
        "event_id": "opening",
        "type": "opening_balance",
        "occurred_at": f"2023-11-02T{opening_clock}+09:00",
        "amount_yen": 1000,
    }
    cost = {
        "event_id": "cost",
        "type": "cost",
        "occurred_at": f"{end}T{clock}+09:00",
        "cost_kind": "commission",
        "amount_yen": 10,
    }
    if reported:
        cost["occurred_on"] = end
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": f"{end}T18:00:00+09:00",
            "events": [opening, cost],
        }
    )
    return compute_portfolio_outcome(
        ledger,
        benchmark(end),
        business_days=(date(2023, 11, 2), date.fromisoformat(end)),
        bars=(),
    )


@pytest.mark.parametrize(
    ("end", "clock", "expected_cost"),
    [
        ("2024-11-01", "14:59:59", 10),
        ("2024-11-01", "15:00:00", 10),
        ("2024-11-01", "15:00:01", 0),
        ("2024-11-01", "15:10:00", 0),
        ("2024-11-05", "15:10:00", 10),
        ("2024-11-05", "15:30:00", 10),
        ("2024-11-05", "15:30:01", 0),
    ],
)
def test_confirmed_cost_uses_its_session_close(end, clock, expected_cost):
    result = outcome(end=end, clock=clock)
    assert result.status == "resolved"
    assert result.confirmed_cost_yen == expected_cost
    assert result.ending_cash_yen == 1000 - expected_cost
    assert result.portfolio_twr_pct == pytest.approx(-expected_cost / 10)


def test_reported_day_keeps_its_date_only_meaning_after_the_clock_rule_changes():
    result = outcome(reported=True)
    assert result.confirmed_cost_yen == 10
    assert result.ending_cash_yen == 990


@pytest.mark.parametrize(
    ("clock", "status"), [("15:00:00", "resolved"), ("15:00:01", "unresolved")]
)
def test_opening_balance_must_also_exist_by_the_historical_opening_close(clock, status):
    assert outcome(opening_clock=clock).status == status


def test_recalculation_does_not_rewrite_an_outcome_saved_with_the_old_close_rule(tmp_path):
    current = outcome()
    historical = replace(
        current,
        confirmed_cost_yen=10,
        ending_cash_yen=990,
        portfolio_twr_pct=-1.0,
        excess_percentage_points=-1.0,
    )
    payload = outcome_to_payload(historical)
    payload.update(
        benchmark_id=benchmark().benchmark_id,
        benchmark_observation=benchmark().model_dump(mode="json"),
        market_data_ref="synthetic:historical-close",
        market_data_coverage_start_date=historical.period_start_date.isoformat(),
        market_data_coverage_end_date=historical.period_end_date.isoformat(),
    )
    saved = PortfolioOutcomePublication(
        outcome_id="old-close-result",
        horizon=historical.horizon,
        period_start_date=historical.period_start_date.isoformat(),
        period_end_date=historical.period_end_date.isoformat(),
        status=historical.status,
        payload=payload,
    )
    path = tmp_path / "application.sqlite"
    store = PortfolioOutcomeStore(path)
    assert store.publish(saved)
    original = store.list()[0]
    before = path.read_bytes()
    assert not store.publish(saved)
    corrected = replace(saved, payload={**payload, **outcome_to_payload(current)})
    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(corrected)
    assert path.read_bytes() == before
    assert store.list() == (original,)
    assert store.publish(replace(corrected, outcome_id="corrected-close-result"))
    rows = {row["outcome_id"]: row for row in store.list()}
    assert rows[saved.outcome_id] == original
    assert rows["corrected-close-result"]["ending_cash_yen"] == 1000
