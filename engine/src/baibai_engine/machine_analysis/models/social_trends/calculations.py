"""範囲・帰属ごとの主張とその明示差。予定日は実施確認へ昇格させない。"""

from __future__ import annotations

from datetime import date

from baibai_engine.machine_analysis.models.trend_payload import claim_record, finish_payload
from baibai_engine.machine_analysis.observations.trends.inputs import FixedTrendInputs

REVISION = "social-trends-v1"


def calculate_social_trends(fixed: FixedTrendInputs, *, as_of: date) -> dict[str, object]:
    records: list[dict[str, object]] = []
    comparisons: list[dict[str, object]] = []
    for selected in fixed.artifacts:
        artifact = selected.current
        for claim in artifact.claims:
            if claim.domain != "social":
                continue
            record = claim_record(claim, artifact)
            record["schedule_status"] = (
                "date_reached_implementation_unconfirmed"
                if claim.status == "scheduled"
                and not claim.negated
                and claim.scheduled_for is not None
                and claim.scheduled_for <= as_of
                else "future_schedule"
                if claim.status == "scheduled" and claim.scheduled_for and not claim.negated
                else None
            )
            records.append(record)
            if selected.baseline is None:
                continue
            for previous in selected.baseline.claims:
                if (
                    previous.domain != "social"
                    or previous.scope != claim.scope
                    or previous.predicate != claim.predicate
                    or previous.attribution != claim.attribution
                    or (
                        previous.status != claim.status
                        and not (
                            claim.explicit_event_id is not None
                            and claim.explicit_event_id == previous.explicit_event_id
                        )
                    )
                ):
                    continue
                # An explicit baseline compares reports, not inferred same-event
                # identity. Conflicting or late claims remain individually readable.
                comparison = {
                    "kind": "reported_claim_difference",
                    "same_explicit_event": claim.explicit_event_id is not None
                    and claim.explicit_event_id == previous.explicit_event_id,
                    "current": record,
                    "baseline": claim_record(previous, selected.baseline),
                    "changed": (
                        claim.quantity,
                        claim.status,
                        claim.negated,
                        claim.conditions,
                        claim.scheduled_for,
                        claim.uncertainty,
                    )
                    != (
                        previous.quantity,
                        previous.status,
                        previous.negated,
                        previous.conditions,
                        previous.scheduled_for,
                        previous.uncertainty,
                    ),
                    "state_superseded": False,
                }
                if (
                    claim.quantity is not None
                    and previous.quantity is not None
                    and claim.quantity.unit == previous.quantity.unit
                    and claim.quantity.basis == previous.quantity.basis
                    and not claim.negated
                    and not previous.negated
                    and claim.conditions == previous.conditions
                ):
                    comparison["reported_quantity_delta"] = {
                        "value": str(claim.quantity.value - previous.quantity.value),
                        "unit": "percentage_points"
                        if claim.quantity.unit == "percent"
                        else claim.quantity.unit,
                    }
                comparisons.append(comparison)
    return finish_payload(
        fixed,
        records=records,
        comparisons=comparisons,
        context={
            "comparison_status": "no_current_extracted_claims"
            if not records
            else "baseline_missing"
            if not comparisons
            else "available",
            "limitations": [
                "initial_coverage_boj_policy_decisions",
                "no_importance_or_inferred_causality",
                "unlinked_or_conflicting_claims_are_retained",
            ],
        },
    )
