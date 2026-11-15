from datetime import date, timedelta
from decimal import Decimal

import pytest
from tests.helpers.trends import BOJ, NOW, document, fixed

from baibai_engine.machine_analysis.models.social_trends.calculations import calculate_social_trends
from baibai_engine.machine_analysis.observations.trends.contracts import ExtractionArtifact
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


@pytest.mark.parametrize(
    ("previous_kind", "current_kind", "previous_date", "current_date", "changed"),
    [
        ("forecast_claim", "reported_fact", date(2026, 9, 18), date(2026, 9, 18), True),
        ("reported_fact", "forecast_claim", date(2026, 9, 18), date(2026, 9, 18), True),
        ("reported_fact", "reported_fact", date(2026, 9, 18), date(2026, 10, 20), True),
        ("reported_fact", "reported_fact", None, date(2026, 9, 18), True),
        ("reported_fact", "reported_fact", date(2026, 9, 18), None, True),
        ("reported_fact", "reported_fact", None, None, False),
        ("reported_fact", "reported_fact", date(2026, 9, 18), date(2026, 9, 18), False),
    ],
)
def test_reported_kind_and_occurrence_changes_are_not_quantity_or_state_changes(
    previous_kind, current_kind, previous_date, current_date, changed
):
    def artifact(kind, occurred_on):
        raw = decision().model_dump(mode="python")
        raw["claims"] = [
            {**raw["claims"][0], "kind": kind, "status": "reported", "occurred_on": occurred_on}
        ]
        return ExtractionArtifact.model_validate(raw)

    previous = artifact(previous_kind, previous_date)
    current = artifact(current_kind, current_date)
    result = calculate_social_trends(fixed(current, baseline=previous), as_of=date(2026, 10, 25))
    (comparison,) = result["comparisons"]
    assert comparison["changed"] is changed
    assert comparison["current"]["claim"] == current.claims[0].model_dump(mode="json")
    assert comparison["baseline"]["claim"] == previous.claims[0].model_dump(mode="json")
    assert comparison["current"]["artifact"] == current.ref.model_dump(mode="json")
    assert comparison["baseline"]["artifact"] == previous.ref.model_dump(mode="json")
    assert comparison["reported_quantity_delta"] == {"value": "0.00", "unit": "percentage_points"}
    assert comparison["same_explicit_event"] is False
    assert comparison["state_superseded"] is False


@pytest.mark.parametrize(
    "field", ["received_at", "published_at", "created_at", "source_url", "spans"]
)
def test_receipt_extraction_and_citation_changes_do_not_change_reported_content(field):
    current = decision()
    prior_raw = current.model_dump(mode="python")
    if field == "created_at":
        prior_raw[field] = NOW - timedelta(days=1)
    elif field == "spans":
        prior_raw["claims"][0][field] = [{"start": 0, "end": 1}]
    else:
        prior_raw["evidence"][field] = (
            "https://example.invalid/prior" if field == "source_url" else NOW - timedelta(days=1)
        )
    previous = ExtractionArtifact.model_validate(prior_raw)
    result = calculate_social_trends(fixed(current, baseline=previous), as_of=date(2026, 10, 25))
    assert result["comparisons"]
    for comparison in result["comparisons"]:
        assert comparison["changed"] is False
        assert comparison["state_superseded"] is False
        assert comparison["current"]["artifact"] == current.ref.model_dump(mode="json")
        assert comparison["baseline"]["artifact"] == previous.ref.model_dump(mode="json")


def test_schedule_passage_changes_display_not_the_reported_claim():
    artifact = decision()
    inputs = fixed(artifact, baseline=artifact)
    before = calculate_social_trends(inputs, as_of=date(2026, 9, 23))
    after = calculate_social_trends(inputs, as_of=date(2026, 9, 24))
    for result, status in (
        (before, "future_schedule"),
        (after, "date_reached_implementation_unconfirmed"),
    ):
        scheduled = next(
            row for row in result["comparisons"] if row["current"]["claim"]["status"] == "scheduled"
        )
        assert scheduled["current"]["schedule_status"] == status
        assert scheduled["changed"] is False
        assert scheduled["state_superseded"] is False
