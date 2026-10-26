from datetime import UTC, date, datetime

import pytest

from baibai_engine.machine_analysis.model_generation import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy


def generation(*models):
    return ModelGeneration(
        generation_id="fixed-generation",
        as_of=date(2026, 10, 23),
        evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="fixed-market", sha256="a" * 64)
        },
        models=models,
    )


def value(ticker, earnings):
    return GeneratedModel(
        model_id="company_valuation",
        subject=ticker,
        calculation_revision="synthetic-v1",
        payload={"earnings_price": {"earnings_yield": earnings}},
    )


def test_new_model_does_not_redefine_policy_or_universe():
    original = generation(value("1234", 0.10), value("5678", 0.09))
    extended = generation(
        *original.models,
        value("9999", 10.0),
        GeneratedModel(
            model_id="future_model",
            subject="5678",
            calculation_revision="v1",
            payload={"score": 999},
        ),
    )
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    first = select_generation(original, tickers=("1234", "5678"), policy=policy)
    second = select_generation(extended, tickers=("1234", "5678"), policy=policy)
    assert first == second
    assert first.generation_id == original.generation_id
    assert first.universe_tickers == ("1234", "5678")
    assert first.result.tickers == ("1234",)
    assert first.result.views[1].eligible_count == 0


def test_missing_technical_model_does_not_remove_the_earnings_price_view():
    saved = generation(value("1234", 0.10))
    selected = select_generation(
        saved, tickers=("1234", "5678"), policy=TwoViewPolicy(earnings_limit=2, combined_limit=2)
    )
    assert selected.result.tickers == ("1234",)
    assert selected.result.views[0].eligible_count == 1
    assert selected.result.views[1].eligible_count == 0


def test_malformed_known_model_is_not_misreported_as_missing_observation():
    malformed = GeneratedModel(
        model_id="company_valuation",
        subject="1234",
        calculation_revision="v1",
        payload={},
    )
    with pytest.raises(ValueError, match=r"earnings_price\.earnings_yield"):
        select_generation(
            generation(malformed),
            tickers=("1234",),
            policy=TwoViewPolicy(earnings_limit=1, combined_limit=1),
        )


def test_generated_observations_drive_the_two_views_without_rounding():
    saved = generation(
        value("1234", 0.100049),
        value("5678", 0.100041),
        *(
            GeneratedModel(
                model_id="technical_analysis",
                subject=ticker,
                calculation_revision="synthetic-v1",
                payload={"price_momentum": {"value": momentum}},
            )
            for ticker, momentum in (("1234", -0.5), ("5678", 0.5))
        ),
    )
    result = select_generation(
        saved, tickers=("1234", "5678"), policy=TwoViewPolicy(earnings_limit=1, combined_limit=1)
    ).result
    assert result.views[0].companies[0].ticker == "1234"
    assert result.views[0].companies[0].ranking_value == 0.100049
    assert result.views[1].companies[0].ticker == "5678"
    assert result.tickers == ("1234", "5678")


def test_price_path_annotation_cannot_change_y_or_ym_selection():
    original = generation(
        value("1234", 0.10),
        value("5678", 0.09),
        *(
            GeneratedModel(
                model_id="technical_analysis",
                subject=ticker,
                calculation_revision="legacy",
                payload={"price_momentum": {"value": momentum}},
            )
            for ticker, momentum in (("1234", -0.5), ("5678", 0.5))
        ),
    )
    extended = generation(
        *(
            model.model_copy(
                update={
                    "calculation_revision": "with-price-path",
                    "payload": {
                        **model.payload,
                        "price_path": {
                            "annotation": {"status": "matched", "matched_refs": ["G60"]}
                        },
                    },
                }
            )
            if model.model_id == "technical_analysis"
            else model
            for model in original.models
        )
    )
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    assert select_generation(
        original, tickers=("1234", "5678"), policy=policy
    ) == select_generation(extended, tickers=("1234", "5678"), policy=policy)


def test_selection_retains_the_requested_universe_including_missing_models():
    saved = generation(value("1234", 0.10))
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    full = select_generation(saved, tickers=("5678", "1234"), policy=policy)
    reordered = select_generation(saved, tickers=("1234", "5678"), policy=policy)
    narrow = select_generation(saved, tickers=("1234",), policy=policy)
    assert full == reordered
    assert full.result == narrow.result
    assert full != narrow
    assert full.universe_tickers == ("1234", "5678")


def test_duplicate_normalized_subjects_are_not_silently_deduplicated():
    with pytest.raises(ValueError, match="one row per ticker"):
        select_generation(
            generation(value("1234", 0.10)),
            tickers=("1234", " 1234 "),
            policy=TwoViewPolicy(earnings_limit=1, combined_limit=1),
        )
