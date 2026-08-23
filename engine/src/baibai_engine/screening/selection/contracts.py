"""Explicit Opportunity Discovery identities for the adopted Value / Carry wire contract.

This module deliberately is not a registry or a policy DSL.  It gives the two
implemented policies one canonical representation so a stored selection can say
exactly which executable contract produced it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.appdb.json import canonical_json
from baibai_engine.screening.estimates import EXPECTED_RETURN_MODEL_VERSION

VALUE_CARRY_OPPORTUNITY_LANE_ID = "value-carry"
VALUE_CARRY_SELECTION_POLICY_ID = "value-carry-v1"
VALUE_CARRY_ONLY_ATTENTION_POLICY_ID = "value-carry-only-v1"
RESEARCH_GATE_CONTRACT_ID = "research-gate-v1"

COMMON_INVESTABLE_GATE_ID = "required-jpx-flags-and-liquidity-v1"


class ValueCarryOnlyAttentionParameters(BaseModel):
    """Runtime parameters accepted by the Core-only Attention Policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    value_carry_limit: int = Field(ge=0, le=100)


def value_carry_selection_policy_representation(
    *,
    lane_longlist_depth: int,
    screening_rules_hash: str | None,
    required_jpx_flags: Sequence[str],
    liquidity_parameters: Mapping[str, object],
    evidence_pattern_order: Sequence[str],
) -> dict[str, object]:
    """Return the exact executable Value / Carry v1 contract."""

    return {
        "opportunity_lane_id": VALUE_CARRY_OPPORTUNITY_LANE_ID,
        "selection_policy_id": VALUE_CARRY_SELECTION_POLICY_ID,
        "expected_return_model_id": EXPECTED_RETURN_MODEL_VERSION,
        "screening_rules_hash": screening_rules_hash,
        "common_investable_gate": {
            "common_investable_gate_id": COMMON_INVESTABLE_GATE_ID,
            "required_jpx_flags": sorted(required_jpx_flags),
            "liquidity_parameters": dict(liquidity_parameters),
        },
        "eligibility": {"er_annual": "finite"},
        "ordering": [
            "er_annual_desc",
            "primary_evidence_pattern_order",
            "evidence_strength",
            "ticker_asc",
        ],
        "evidence_pattern_order": list(evidence_pattern_order),
        "lane_longlist_depth": lane_longlist_depth,
    }


def value_carry_selection_policy_hash(
    *,
    lane_longlist_depth: int,
    screening_rules_hash: str | None,
    required_jpx_flags: Sequence[str],
    liquidity_parameters: Mapping[str, object],
    evidence_pattern_order: Sequence[str],
) -> str:
    return _canonical_sha256(
        value_carry_selection_policy_representation(
            lane_longlist_depth=lane_longlist_depth,
            screening_rules_hash=screening_rules_hash,
            required_jpx_flags=required_jpx_flags,
            liquidity_parameters=liquidity_parameters,
            evidence_pattern_order=evidence_pattern_order,
        )
    )


def value_carry_only_attention_policy_hash(
    *,
    selection_policy_hash: str,
    parameters: ValueCarryOnlyAttentionParameters,
) -> str:
    """Hash policy identity and runtime parameters, excluding dynamic Review Basis."""

    return _canonical_sha256(
        {
            "attention_policy_id": VALUE_CARRY_ONLY_ATTENTION_POLICY_ID,
            "input_selection_policy_hash": selection_policy_hash,
            "parameters": parameters.model_dump(mode="json"),
            "review_set_construction": "value-carry-lane-longlist-in-order",
        }
    )


def _canonical_sha256(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "RESEARCH_GATE_CONTRACT_ID",
    "VALUE_CARRY_ONLY_ATTENTION_POLICY_ID",
    "VALUE_CARRY_OPPORTUNITY_LANE_ID",
    "VALUE_CARRY_SELECTION_POLICY_ID",
    "ValueCarryOnlyAttentionParameters",
    "value_carry_only_attention_policy_hash",
    "value_carry_selection_policy_hash",
    "value_carry_selection_policy_representation",
]
