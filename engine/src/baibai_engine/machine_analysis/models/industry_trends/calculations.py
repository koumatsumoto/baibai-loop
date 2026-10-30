"""比較可能な業績と明示的な事業構造の観測。重要度・将来利益を判断しない。"""

from __future__ import annotations

from typing import Literal

from baibai_engine.machine_analysis.observations.trends.contracts import (
    Claim,
    ExtractionArtifact,
)
from baibai_engine.machine_analysis.observations.trends.inputs import IndustryInput
from baibai_engine.machine_analysis.models.trend_payload import (
    claim_record,
    finish_payload,
)

REVISION = "industry-trends-v1"
type ComparisonKind = Literal["period_change", "restatement", "extraction_change", "not_comparable"]


def compare_claims(
    current: Claim,
    previous: Claim,
    *,
    current_artifact: ExtractionArtifact,
    previous_artifact: ExtractionArtifact,
) -> dict[str, object]:
    """母集団等の不一致は差分を計算しない。同一期の版差を成長率にしない。"""
    left = previous.scope.model_dump(exclude={"period", "period_start", "period_end"})
    right = current.scope.model_dump(exclude={"period", "period_start", "period_end"})
    quantity, old = current.quantity, previous.quantity
    kind: ComparisonKind = "not_comparable"
    reason = "different_scope_or_basis"
    delta = None
    unit = None
    comparable = (
        current.kind == previous.kind == "reported_fact"
        and not current.negated
        and not previous.negated
        and current.conditions == previous.conditions
        and current.uncertainty == previous.uncertainty
        and current.predicate == previous.predicate
        and current.attribution == previous.attribution
        and left == right
        and current.scope.population is not None
        and current.scope.population_id is not None
        and current.scope.period is not None
        and previous.scope.period is not None
        and quantity is not None
        and old is not None
        and quantity.unit == old.unit
        and quantity.basis == old.basis
    )
    if comparable and quantity is not None and old is not None:
        same_period = (
            current.scope.period,
            current.scope.period_start,
            current.scope.period_end,
        ) == (previous.scope.period, previous.scope.period_start, previous.scope.period_end)
        same_raw = current_artifact.evidence.raw_sha256 == previous_artifact.evidence.raw_sha256
        different_extraction = (
            current_artifact.method_revision != previous_artifact.method_revision
            or current_artifact.method != previous_artifact.method
            or current_artifact.assistant_config != previous_artifact.assistant_config
        )
        if different_extraction:
            kind, reason = "extraction_change", "extraction_methods_differ"
        elif same_period:
            kind, reason = "restatement", "same_period_evidence_revision"
            if same_raw and current_artifact.ref == previous_artifact.ref:
                reason = "same_artifact_reported_values"
        elif (
            current.scope.period_end is not None
            and previous.scope.period_end is not None
            and current.scope.period_end > previous.scope.period_end
        ):
            kind, reason = "period_change", "comparable_reported_periods"
        else:
            reason = "period_order_unknown_or_reversed"
        if kind != "not_comparable":
            delta = str(quantity.value - old.value)
            unit = "percentage_points" if quantity.unit == "percent" else quantity.unit
    return {
        "kind": kind,
        "reason": reason,
        "delta": delta,
        "delta_unit": unit,
        "current": claim_record(current, current_artifact),
        "baseline": claim_record(previous, previous_artifact),
    }


def calculate_industry_trends(fixed: IndustryInput) -> dict[str, object]:
    records: list[dict[str, object]] = []
    comparisons: list[dict[str, object]] = []
    for selected in fixed.inputs.artifacts:
        artifact = selected.current
        claims = [
            c
            for c in artifact.claims
            if c.domain == "industry" and c.scope.scope_id == fixed.definition.scope_id
        ]
        records.extend(claim_record(claim, artifact) for claim in claims)
        # Explicitly reported previous-period values within the same survey are
        # usable; otherwise the caller must name an immutable baseline artifact.
        for claim in claims:
            within = [
                c
                for c in claims
                if c.predicate == claim.predicate
                and c.scope.period_end is not None
                and claim.scope.period_end is not None
                and c.scope.period_end < claim.scope.period_end
                and compare_claims(
                    claim,
                    c,
                    current_artifact=artifact,
                    previous_artifact=artifact,
                )["kind"]
                == "period_change"
            ]
            # A reported prior year in the same survey is the closest explicit
            # comparison. An index artifact is not a replacement for that claim.
            if within:
                latest_period = max(
                    c.scope.period_end for c in within if c.scope.period_end is not None
                )
                candidates = [c for c in within if c.scope.period_end == latest_period]
                if len(candidates) != 1:
                    continue  # Conflicting same-period claims need adjudication.
                previous, previous_artifact = candidates[0], artifact
            elif selected.baseline is not None:
                baseline = selected.baseline
                candidates = [
                    c
                    for c in baseline.claims
                    if c.domain == "industry"
                    and c.scope.scope_id == claim.scope.scope_id
                    and c.predicate == claim.predicate
                ]
                if len(candidates) != 1:
                    continue
                previous, previous_artifact = candidates[0], baseline
            else:
                continue
            comparisons.append(
                compare_claims(
                    claim,
                    previous,
                    current_artifact=artifact,
                    previous_artifact=previous_artifact,
                )
            )
    return finish_payload(
        fixed.inputs,
        records=records,
        comparisons=comparisons,
        context={
            "scope": fixed.definition.model_dump(mode="json"),
            "comparison_status": "no_current_extracted_claims"
            if not records
            else "baseline_missing_or_not_applicable"
            if not comparisons
            else "available"
            if any(item["kind"] != "not_comparable" for item in comparisons)
            else "not_comparable",
            "limitations": ["no_inferred_supply_capacity_or_investment_effect"],
        },
    )
