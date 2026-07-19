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
        "reason": None,
        "horizon": "1y",
        "period_start_date": "2025-01-01",
        "period_end_date": "2025-12-31",
        "benchmark_observation": {
            "horizon": "1y",
            "period_start_date": "2025-01-01",
            "period_end_date": "2025-12-31",
            "cumulative_return_pct": 8.2,
        },
    }
    return PortfolioOutcomePublication(
        outcome_id="outcome-1y-2025-12-31",
        horizon="1y",
        period_start_date="2025-01-01",
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
