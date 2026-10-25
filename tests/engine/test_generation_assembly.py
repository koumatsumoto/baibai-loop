"""共有入力の組立と選定の分離。全社/個社でも参照文脈を差し替えない。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.machine_analysis.company_growth.models import InputRelease
from baibai_engine.machine_analysis.company_valuation.metrics.models import TTMRules
from baibai_engine.machine_analysis.external_assessment.models import ObservationHistory
from baibai_engine.machine_analysis.generation import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.model_generation import InputSnapshot, ModelGeneration

AS_OF = date(2026, 10, 23)
NOW = datetime(2026, 10, 25, tzinfo=UTC)
RELEASE = InputRelease(release_id="synthetic", manifest_sha256="a" * 64)
RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=370,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


def generate(tickers=("1001", "1002"), **updates):
    values = {
        "generation_id": "synthetic",
        "as_of": AS_OF,
        "evaluation_time": NOW,
        "inputs": {
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="a" * 64)
        },
        "financial_release": RELEASE,
        "companies": tuple(CompanyModelInputs(t, (), (), (), None) for t in tickers),
        "trading_dates": (),
        "expected_price_date": AS_OF,
        "ttm_rules": RULES,
        "external_observations": {t: ObservationHistory(ticker=t, cutoff=NOW) for t in tickers},
        "market_context": {"reading": None, "market": {"reference_population": "all-stocks"}},
        "securities_context": {t: {"peer_count": 12} for t in tickers},
        "market_context_revision": "synthetic-v1",
    }
    return generate_models(**(values | updates))


def test_five_models_are_generated_once_per_subject_and_common_market_once():
    result = generate()
    assert len(result.models) == 11
    assert ModelGeneration.model_validate_json(result.model_dump_json()) == result
    individual = generate(tickers=("1001",))
    assert individual.models == tuple(m for m in result.models if m.subject in ("1001", "market"))
    assert (
        result.result("company_valuation", "1001").payload["earnings_price"]["earnings_yield"]
        is None
    )
    assert "machine_valuation" not in result.result("company_valuation", "1001").payload


def test_omitted_optional_input_does_not_remove_other_models():
    normal = generate()
    missing = generate(external_observations={}, securities_context={}, market_context=None)
    assert len(missing.models) == 6
    assert all(normal.result(m.model_id, m.subject) == m for m in missing.models)
    assert missing.result("external_assessment", "1001") is None


def test_unrelated_release_and_duplicate_subjects_are_rejected():
    with pytest.raises(ValueError, match="financial release"):
        generate(
            inputs={
                "market": InputSnapshot(kind="lake_release", reference="other", sha256="b" * 64)
            }
        )
    with pytest.raises(ValueError, match="unique companies"):
        generate(tickers=("1001", "1001"))
    with pytest.raises(ValueError, match="identity or cutoff"):
        generate(
            external_observations={
                "1001": ObservationHistory(ticker="1001", cutoff=NOW + timedelta(seconds=1))
            }
        )
