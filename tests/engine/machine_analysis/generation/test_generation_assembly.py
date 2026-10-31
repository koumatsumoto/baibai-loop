"""共有入力の組立と選定の分離。全社/個社でも参照文脈を差し替えない。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGeneration
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    ObservationHistory,
)

AS_OF = date(2026, 10, 23)
NOW = datetime(2026, 10, 25, tzinfo=UTC)
RELEASE = InputRelease(release_id="synthetic", manifest_sha256="a" * 64)
RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=370,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


def generate(tickers=("1001", "1002"), *, stream=False, **updates):
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
    generated = generate_models(**(values | updates))
    return generated if stream else generated.materialize()


def test_five_models_are_generated_once_per_subject_and_common_market_once():
    result = generate()
    assert len(result.models) == 11
    assert ModelGeneration.model_validate_json(result.model_dump_json()) == result
    individual = generate(tickers=("1001",))
    assert individual.models == tuple(m for m in result.models if m.subject in ("1001", "market"))
    assert (
        result.result("company_financials", "1001").payload["earnings_price"]["earnings_yield"]
        is None
    )
    assert "machine_valuation" not in result.result("company_financials", "1001").payload


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


def test_trends_are_shared_and_preserve_all_old_model_payloads(tmp_path):
    from tests.trend_fixtures import generation_inputs

    from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
    from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore

    old = generate()
    stream = generate(stream=True, trends=generation_inputs())
    assert stream.subjects == ("1001", "1002")
    new = stream.materialize()
    assert len(new.models) == 13
    assert len({model.model_id for model in new.models}) == 7
    assert len([plan for plan in new.subject_plan.values() if plan.scope == "stock"]) == 2
    assert new.subject_plan["1001"].model_ids == old.subject_plan["1001"].model_ids
    assert new.subject_plan["industry:jp-pharmaceutical-wholesale"].scope == "sector"
    assert new.subject_plan["market:global"].scope == "market"
    assert all(new.result(model.model_id, model.subject) == model for model in old.models)
    assert old.model_dump_json() == generate().model_dump_json()
    assert (
        new.result("industry_trends", "industry:jp-pharmaceutical-wholesale").payload["coverage"][
            "state"
        ]
        == "not_collected"
    )

    path = tmp_path / "runs.sqlite"
    ModelGenerationStore(path).publish(new)
    # Candidate status is irrelevant; both stocks get their same shared context.
    for ticker in ("1001", "1002"):
        saved = read_research_model_inputs(new.generation_id, targets=(ticker,), path=path)
        projection = project_generated_inputs(saved)
        assert set(projection["securities"]) == {ticker}
        assert "social_trends" in projection["shared_subjects"]["market:global"]["models"]
        assert (
            "industry_trends"
            in projection["shared_subjects"]["industry:jp-pharmaceutical-wholesale"]["models"]
        )
        assert "social_trends" not in projection["securities"][ticker]["not_generated"]
    before = saved.model_dump_json()
    path.unlink()
    assert project_generated_inputs(saved) == projection
    assert saved.model_dump_json() == before


def test_trend_manifest_identity_is_checked_before_lazy_generation():
    from tests.trend_fixtures import fixed, generation_inputs

    trends = generation_inputs().model_copy(update={"social": fixed(release_id="another-release")})
    with pytest.raises(ValueError, match="trend input manifest"):
        generate(trends=trends)
