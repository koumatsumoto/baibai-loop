"""Canonical synthetic outcome shared by persistence and retained Web tests."""

from baibai_engine.portfolio.performance.store import PortfolioOutcomePublication


def _publication(*, status: str = "resolved") -> PortfolioOutcomePublication:
    payload: dict[str, object] = {
        "schema_version": 2,
        "kind": "portfolio_outcome",
        "status": status,
        "reason": None if status == "resolved" else "benchmark_unavailable",
        "horizon": "1y",
        "period_start_date": "2024-12-31",
        "period_end_date": "2025-12-31",
        "benchmark_id": "jpx-topix-gross-total-return",
        "market_data_ref": "stores/market/market.sqlite",
        "market_data_coverage_start_date": "2024-12-31",
        "market_data_coverage_end_date": "2025-12-31",
        "benchmark_observation": {
            "schema_version": 1,
            "kind": "benchmark_observation",
            "benchmark_id": "jpx-topix-gross-total-return",
            "index_name": "TOPIX",
            "dividend_treatment": "gross_total_return",
            "horizon": "1y",
            "period_start_date": "2024-12-31",
            "period_end_date": "2025-12-31",
            "period_basis": "official_explicit",
            "period_rule_source_url": None,
            "cumulative_return_pct": 8.2,
            "annualized_return_pct": None,
            "display_precision_bps": 10,
            "source_url": "https://www.jpx.co.jp/example",
            "source_as_of": "2025-12-31",
            "published_at": "2026-01-01",
            "retrieved_at": "2026-01-01T12:00:00+09:00",
        },
    }
    if status == "resolved":
        payload.update(
            portfolio_twr_pct=10.2,
            benchmark_cumulative_return_pct=8.2,
            excess_percentage_points=2.0,
            portfolio_annualized_return_pct=10.2,
            benchmark_annualized_return_pct=None,
            ending_cash_yen=100,
            ending_reserved_cash_yen=0,
            ending_holdings_market_value_yen=900,
            confirmed_income_yen=10,
            confirmed_cost_yen=2,
            confirmed_tax_yen=1,
            estimated_exit_tax_yen=20,
            estimated_exit_tax_status="estimated",
            open_tickers=["2331"],
            closed_tickers=[],
        )
    return PortfolioOutcomePublication(
        outcome_id="outcome-1y-2025-12-31",
        horizon="1y",
        period_start_date="2024-12-31",
        period_end_date="2025-12-31",
        status=status,
        payload=payload,
    )
