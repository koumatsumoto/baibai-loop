from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.position.outcome_store import (
    PortfolioOutcomeConflictError,
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)


def _publication(*, status: str = "resolved") -> PortfolioOutcomePublication:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "portfolio_outcome",
        "status": status,
        "reason": None if status == "resolved" else "benchmark_unavailable",
        "horizon": "1y",
        "period_start_date": "2024-12-31",
        "period_end_date": "2025-12-31",
        "benchmark_id": "jpx-topix-gross-total-return",
        "market_data_ref": "stores/market/market.sqlite",
        "market_data_sha256": "a" * 64 if status == "resolved" else None,
        "market_data_coverage_start_date": "2024-12-31",
        "market_data_coverage_end_date": "2025-12-31",
        "market_data_fingerprint": "bars-v1" if status == "resolved" else None,
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


def test_outcome_publication_is_create_only_and_queryable(tmp_path: Path) -> None:
    store = PortfolioOutcomeStore(tmp_path / "app.sqlite")
    publication = _publication()

    assert store.publish(publication)
    assert not store.publish(publication)
    assert store.list()[0]["outcome_id"] == publication.outcome_id
    assert store.list()[0]["benchmark_observation"]["cumulative_return_pct"] == 8.2  # type: ignore[index]


def test_outcome_conflict_rolls_back(tmp_path: Path) -> None:
    store = PortfolioOutcomeStore(tmp_path / "app.sqlite")
    store.publish(_publication())

    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(_publication(status="unresolved"))

    assert store.list()[0]["status"] == "resolved"


def test_outcome_requires_embedded_benchmark(tmp_path: Path) -> None:
    publication = _publication()
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={
            key: value
            for key, value in publication.payload.items()
            if key != "benchmark_observation"
        },
    )

    with pytest.raises(ValueError, match="benchmark_observation"):
        PortfolioOutcomeStore(tmp_path / "app.sqlite").publish(invalid)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unexpected": True}, "Extra inputs"),
        ({"excess_percentage_points": 99.0}, "portfolio minus benchmark"),
        ({"benchmark_cumulative_return_pct": 8.1}, "benchmark return"),
        ({"ending_cash_yen": None}, "resolved outcome fields"),
    ],
)
def test_outcome_rejects_retired_schema_and_semantic_bypasses(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    publication = _publication()
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={**publication.payload, **change},
    )

    with pytest.raises(ValueError, match=message):
        PortfolioOutcomeStore(tmp_path / "app.sqlite").publish(invalid)


def test_outcome_rejects_invalid_embedded_benchmark_without_write(tmp_path: Path) -> None:
    publication = _publication()
    benchmark_source = publication.payload["benchmark_observation"]
    assert isinstance(benchmark_source, dict)
    benchmark = dict(benchmark_source)
    benchmark["horizon"] = "5y"
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={**publication.payload, "benchmark_observation": benchmark},
    )
    path = tmp_path / "app.sqlite"

    with pytest.raises(ValueError, match=r"period|horizon"):
        PortfolioOutcomeStore(path).publish(invalid)

    assert PortfolioOutcomeStore(path).list() == ()
