from datetime import datetime
from pathlib import Path

import pytest

from baibai_engine.market.jpx_total_return import (
    BenchmarkObservationError,
    load_benchmark_observation,
)
from baibai_engine.market_context.observations.benchmark import BenchmarkObservation

FIXTURE = Path(__file__).parents[2] / "fixtures" / "benchmark-observation" / "topix-1y.yaml"


def test_loads_official_gross_total_return_observation() -> None:
    observation = load_benchmark_observation(FIXTURE)

    assert observation.benchmark_id == "jpx-topix-gross-total-return"
    assert observation.annualized_return_pct is None


def test_rejects_period_end_mismatch(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(
            'source_as_of: "2026-06-30"', 'source_as_of: "2026-06-29"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkObservationError, match="period_end_date must equal source_as_of"):
        load_benchmark_observation(bad)


def test_three_year_annualized_cross_check_is_enforced(tmp_path: Path) -> None:
    valid = FIXTURE.read_text(encoding="utf-8").replace("horizon: 1y", "horizon: 3y")
    valid = valid.replace('period_start_date: "2025-06-30"', 'period_start_date: "2023-06-30"')
    valid = valid.replace("annualized_return_pct: null", "annualized_return_pct: 3.94")
    path = tmp_path / "topix-3y.yaml"
    path.write_text(valid, encoding="utf-8")

    assert load_benchmark_observation(path).horizon == "3y"
    path.write_text(
        valid.replace("annualized_return_pct: 3.94", "annualized_return_pct: 99"),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkObservationError, match="annualized_return_pct disagrees"):
        load_benchmark_observation(path)


@pytest.mark.parametrize(
    "retrieved_at",
    ["2026-07-01T00:30:00+09:00", "2026-06-30T15:30:00Z", "2026-06-30T08:30:00-07:00"],
)
def test_publication_day_uses_jst_for_equivalent_retrieval_instants(retrieved_at):
    payload = load_benchmark_observation(FIXTURE).model_dump(mode="json")
    payload["retrieved_at"] = retrieved_at
    observation = BenchmarkObservation.model_validate(payload)
    assert observation.retrieved_at == datetime.fromisoformat("2026-07-01T00:30:00+09:00")


def test_publication_after_retrieval_jst_day_is_still_rejected():
    payload = load_benchmark_observation(FIXTURE).model_dump(mode="json")
    payload["retrieved_at"] = "2026-06-30T14:59:00Z"
    with pytest.raises(ValueError, match="published_at cannot be after retrieved_at"):
        BenchmarkObservation.model_validate(payload)
