from datetime import date
from decimal import Decimal

from tests.trend_fixtures import BOJ, NOW, document, fixed

from baibai_engine.machine_analysis.models.social_trends.calculations import calculate_social_trends
from baibai_engine.machine_analysis.observations.trends.parsers import extract_boj_decision


def decision():
    return extract_boj_decision(document(BOJ, source="boj"), created_at=NOW)


def test_date_passage_is_not_implementation_confirmation_and_dates_remain_separate():
    artifact = decision()
    result = calculate_social_trends(fixed(artifact), as_of=date(2026, 10, 25))
    scheduled = next(r for r in result["observations"] if r["claim"]["status"] == "scheduled")
    assert scheduled["schedule_status"] == "date_reached_implementation_unconfirmed"
    assert scheduled["claim"]["kind"] == "announced_plan"
    assert scheduled["claim"]["occurred_on"] == "2026-09-18"
    assert scheduled["claim"]["scheduled_for"] == "2026-09-24"
    assert scheduled["evidence"]["received_at"] != scheduled["claim"]["occurred_on"]
    assert all(r["claim"]["status"] != "implementation_reported" for r in result["observations"])


def test_explicit_baseline_compares_claims_without_merging_events_or_late_overwrite():
    current = decision()
    prior_claim = current.claims[0].model_copy(
        update={
            "quantity": current.claims[0].quantity.model_copy(update={"value": Decimal("1.5")}),
            "occurred_on": date(2026, 7, 1),
        }
    )
    prior = current.model_copy(update={"claims": (prior_claim,)})
    result = calculate_social_trends(fixed(current, baseline=prior), as_of=date(2026, 10, 25))
    (comparison,) = result["comparisons"]
    assert comparison["changed"] is True
    assert comparison["same_explicit_event"] is False
    assert comparison["state_superseded"] is False
    assert comparison["baseline"]["artifact"] == prior.ref.model_dump(mode="json")


def test_scope_specific_partial_changes_and_contradictions_remain_separate():
    artifact = decision()
    original = artifact.claims[1]
    partial = original.model_copy(
        update={
            "claim_id": "partial",
            "negated": True,
            "scope": original.scope.model_copy(update={"target": "one_facility_only"}),
            "conditions": ("subject_to_review",),
            "uncertainty": ("attributed_disputed_report",),
        }
    )
    artifact = artifact.model_copy(update={"claims": (*artifact.claims, partial)})
    result = calculate_social_trends(fixed(artifact), as_of=date(2026, 9, 1))
    assert len(result["observations"]) == 3
    selected = next(
        r["claim"] for r in result["observations"] if r["claim"]["claim_id"] == "partial"
    )
    assert selected["negated"] is True
    assert selected["conditions"] == ["subject_to_review"]
    assert selected["uncertainty"] == ["attributed_disputed_report"]
