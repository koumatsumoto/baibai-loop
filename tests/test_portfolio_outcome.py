from datetime import date
from pathlib import Path

import pytest

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.jpx_total_return import BenchmarkObservation
from baibai_engine.position.cli import main
from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.position.outcome import (
    DailyNav,
    PortfolioOutcomeError,
    compute_time_weighted_return,
)


def test_twr_neutralizes_beginning_of_day_contribution() -> None:
    result = compute_time_weighted_return(
        (
            DailyNav(date(2026, 1, 2), 1_000),
            DailyNav(date(2026, 1, 5), 1_210, external_flow_yen=100),
        ),
        horizon_years=1,
    )

    assert result.cumulative_return_pct == pytest.approx(10.0)
    assert result.annualized_return_pct == pytest.approx(10.0)


def test_twr_rejects_zero_nav_after_withdrawal() -> None:
    with pytest.raises(PortfolioOutcomeError, match="zero_or_negative_nav"):
        compute_time_weighted_return(
            (
                DailyNav(date(2026, 1, 2), 1_000),
                DailyNav(date(2026, 1, 5), 0, external_flow_yen=-1_000),
            )
        )


def _benchmark() -> BenchmarkObservation:
    return BenchmarkObservation.model_validate(
        {
            "schema_version": 1,
            "kind": "benchmark_observation",
            "benchmark_id": "jpx-topix-gross-total-return",
            "index_name": "TOPIX",
            "dividend_treatment": "gross_total_return",
            "horizon": "1y",
            "period_start_date": "2025-01-06",
            "period_end_date": "2026-01-06",
            "period_basis": "official_explicit",
            "period_rule_source_url": None,
            "cumulative_return_pct": 8.0,
            "annualized_return_pct": None,
            "display_precision_bps": 10,
            "source_url": "https://www.jpx.co.jp/example",
            "source_as_of": "2026-01-06",
            "published_at": "2026-01-07",
            "retrieved_at": "2026-01-08T00:00:00+09:00",
        }
    )


def _ledger() -> PortfolioLedgerDocument:
    return PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 2,
            "portfolio_scope": "repository_only",
            "as_of": "2026-01-06T16:00:00+09:00",
            "estimated_exit_tax_rate_bps": 2000,
            "estimated_exit_tax_basis": "ledger_fifo_gross_unrealized_gain",
            "market_prices": [],
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": "2025-01-06T09:00:00+09:00",
                    "amount_yen": 1000,
                },
                {
                    "event_id": "reserve",
                    "type": "reservation",
                    "occurred_at": "2025-01-06T09:01:00+09:00",
                    "reservation_id": "r1",
                    "order_id": "o1",
                    "ticker": "1234",
                    "sector": "test",
                    "common_factors": [],
                    "quantity": 100,
                    "price_guard_yen": 5,
                    "expires_at": "2025-01-07T09:00:00+09:00",
                },
                {
                    "event_id": "buy",
                    "type": "execution",
                    "occurred_at": "2025-01-06T09:02:00+09:00",
                    "execution_id": "x1",
                    "reservation_id": "r1",
                    "ticker": "1234",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": 5,
                },
                {
                    "event_id": "contribution-weekend",
                    "type": "contribution",
                    "occurred_at": "2026-01-04T09:00:00+09:00",
                    "amount_yen": 100,
                },
                {
                    "event_id": "income",
                    "type": "income",
                    "occurred_at": "2026-01-06T09:00:00+09:00",
                    "ticker": "1234",
                    "income_kind": "dividend",
                    "amount_yen": 20,
                },
                {
                    "event_id": "fee",
                    "type": "cost",
                    "occurred_at": "2026-01-06T09:01:00+09:00",
                    "ticker": "1234",
                    "cost_kind": "commission",
                    "amount_yen": 10,
                },
                {
                    "event_id": "tax",
                    "type": "tax_confirmed",
                    "occurred_at": "2026-01-06T09:02:00+09:00",
                    "ticker": "1234",
                    "tax_kind": "dividend",
                    "amount_yen": 2,
                },
            ],
        }
    )


def test_portfolio_outcome_includes_idle_cash_and_confirmed_cashflows() -> None:
    from baibai_engine.position.outcome import compute_portfolio_outcome

    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None),
        ),
    )

    assert result.status == "resolved"
    assert result.portfolio_twr_pct == pytest.approx((1208 / 1100 - 1) * 100)
    assert result.excess_percentage_points == pytest.approx((1208 / 1100 - 1) * 100 - 8)
    assert result.confirmed_income_yen == 20
    assert result.confirmed_cost_yen == 10
    assert result.confirmed_tax_yen == 2
    # Estimated future exit tax is reported but must not reduce the realized NAV.
    assert result.estimated_exit_tax_yen == 20


def test_portfolio_outcome_rejects_unresolved_corporate_action() -> None:
    from baibai_engine.position.outcome import compute_portfolio_outcome

    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=2.0),
        ),
    )

    assert result.status == "unresolved"
    assert result.reason == "corporate_action_unresolved"


def test_after_close_internal_cashflow_rolls_past_that_close() -> None:
    from baibai_engine.position.outcome import compute_portfolio_outcome

    raw = _ledger().model_dump(mode="json")
    # Move the confirmed tax after close; it must not affect this period's NAV.
    raw["events"][6]["occurred_at"] = "2026-01-06T16:00:00+09:00"
    ledger = PortfolioLedgerDocument.model_validate(raw)
    result = compute_portfolio_outcome(
        ledger,
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None),
        ),
    )

    assert result.status == "resolved"
    assert result.confirmed_tax_yen == 0


def test_outcome_cli_requires_an_imported_application_db_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observation = tmp_path / "topix.yaml"
    observation.write_text(
        (Path(__file__).parent / "fixtures" / "benchmark-observation" / "topix-1y.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    assert main(["outcome", "--root", str(tmp_path), "--benchmark-observation", "topix.yaml"]) == 2
    assert "ledger has not been imported" in capsys.readouterr().err
