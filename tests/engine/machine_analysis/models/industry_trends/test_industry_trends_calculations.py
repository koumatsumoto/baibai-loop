from datetime import timedelta
from decimal import Decimal

import pytest
from tests.trend_fixtures import NOW, PHARMA, document, fixed, generation_inputs

from baibai_engine.machine_analysis.models.industry_trends.calculations import (
    calculate_industry_trends,
    compare_claims,
)
from baibai_engine.machine_analysis.observations.trends.contracts import (
    Claim,
    ExtractionArtifact,
    Quantity,
    SelectedArtifact,
)
from baibai_engine.machine_analysis.observations.trends.inputs import FixedTrendInputs
from baibai_engine.machine_analysis.observations.trends.parsers import extract_tsr_performance
from baibai_engine.machine_analysis.models.trend_payload import MAX_BYTES, MAX_RECORDS
from baibai_engine.primitives.json import canonical_json


def survey():
    return extract_tsr_performance(document(PHARMA), created_at=NOW)


def test_same_survey_period_change_is_percentage_points_with_both_precisions():
    artifact = survey()
    inputs = generation_inputs(artifact).industries[0]
    result = calculate_industry_trends(inputs)
    (comparison,) = result["comparisons"]
    assert comparison["kind"] == "period_change"
    assert comparison["delta"] == "-2.0"
    assert comparison["delta_unit"] == "percentage_points"
    assert comparison["current"]["claim"]["quantity"]["resolution"] == "0.1"
    assert result["input_manifest"]["selected"][0] == artifact.ref.model_dump(mode="json")


def test_population_changes_unknown_membership_and_conditions_are_not_comparable():
    artifact = survey()
    current, previous = artifact.claims[1:3]
    for updates in (
        {"population_size": 777},
        {"population_id": "other-study"},
        {"population_id": None},
    ):
        changed = current.model_copy(update={"scope": current.scope.model_copy(update=updates)})
        result = compare_claims(
            changed, previous, current_artifact=artifact, previous_artifact=artifact
        )
        assert result["kind"] == "not_comparable"
        assert result["delta"] is None
    negated = current.model_copy(update={"negated": True})
    assert (
        compare_claims(negated, previous, current_artifact=artifact, previous_artifact=artifact)[
            "delta"
        ]
        is None
    )


def test_same_period_correction_and_extraction_change_are_not_industry_growth():
    artifact = survey()
    claim = artifact.claims[0]
    corrected = claim.model_copy(update={"quantity": Quantity(value=Decimal("42"), unit="JPY")})
    revision = artifact.model_copy(
        update={
            "evidence": artifact.evidence.model_copy(update={"raw_sha256": "b" * 64}),
            "claims": (corrected,),
        }
    )
    assert (
        compare_claims(corrected, claim, current_artifact=revision, previous_artifact=artifact)[
            "kind"
        ]
        == "restatement"
    )
    extraction = revision.model_copy(update={"method_revision": "another-rule"})
    assert (
        compare_claims(corrected, claim, current_artifact=extraction, previous_artifact=artifact)[
            "kind"
        ]
        == "extraction_change"
    )


def test_failed_new_extraction_keeps_failure_and_separate_last_known_reference():
    old = survey()
    failed = old.model_copy(update={"claims": (), "status": "failed", "method_revision": "next"})
    industry = (
        generation_inputs().industries[0].model_copy(update={"inputs": fixed(failed, baseline=old)})
    )
    result = calculate_industry_trends(industry)
    assert result["observations"] == []
    assert result["comparisons"] == []
    assert result["extraction_states"][0]["status"] == "failed"
    assert result["extraction_states"][0]["last_known"] == old.ref.model_dump(mode="json")


def test_explicit_reorganisation_claims_do_not_infer_supply_destruction():
    artifact = survey()
    base = artifact.claims[0].model_dump()
    claims = tuple(
        Claim.model_validate(
            base
            | {
                "claim_id": predicate,
                "predicate": predicate,
                "quantity": None,
            }
        )
        for predicate in (
            "legal_rehabilitation_filed",
            "business_transferred",
            "operations_continued",
        )
    )
    artifact = ExtractionArtifact.model_validate(artifact.model_dump() | {"claims": claims})
    result = calculate_industry_trends(generation_inputs(artifact).industries[0])
    assert {r["claim"]["predicate"] for r in result["observations"]} == {
        "legal_rehabilitation_filed",
        "business_transferred",
        "operations_continued",
    }
    assert result["comparisons"] == []
    assert "no_inferred_supply_capacity_or_investment_effect" in result["limitations"]


def test_payload_is_bounded_stable_and_omission_preserves_detail_refs():
    artifact = survey()
    artifact = artifact.model_copy(
        update={
            "claims": tuple(
                artifact.claims[0].model_copy(
                    update={"claim_id": f"claim-{i:03d}", "predicate": f"predicate-{i}"}
                )
                for i in range(100)
            )
        }
    )
    industry = generation_inputs(artifact).industries[0]
    result = calculate_industry_trends(industry)
    assert len(result["observations"]) <= MAX_RECORDS
    assert len(canonical_json(result).encode()) <= MAX_BYTES
    assert result["display"]["omitted_observations"] == 100 - len(result["observations"])
    assert result["detail"]["release_id"] == "synthetic"
    assert result == calculate_industry_trends(industry)


def test_fixed_input_rejects_substitution_or_future_artifacts():
    artifact = survey()
    fixed_inputs = fixed(artifact)
    substituted = artifact.model_copy(update={"method_revision": "next"})
    with pytest.raises(ValueError, match="manifest"):
        FixedTrendInputs.model_validate(
            fixed_inputs.model_dump()
            | {
                "artifacts": (SelectedArtifact(current=substituted),),
            }
        )
    with pytest.raises(ValueError, match="cutoff"):
        fixed(artifact.model_copy(update={"created_at": NOW + timedelta(seconds=1)}))


def test_current_survey_prior_year_takes_precedence_over_unsupported_index_baseline():
    from tests.trend_fixtures import generation_inputs

    current = survey()
    old_index = current.model_copy(update={"claims": (), "status": "unsupported"})
    industry = (
        generation_inputs()
        .industries[0]
        .model_copy(update={"inputs": fixed(current, baseline=old_index)})
    )
    result = calculate_industry_trends(industry)
    assert len(result["comparisons"]) == 1
    (comparison,) = result["comparisons"]
    assert comparison["kind"] == "period_change"
    assert comparison["delta"] == "-2.0"
    assert comparison["baseline"]["artifact"] == current.ref.model_dump(mode="json")
