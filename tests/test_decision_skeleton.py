from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import tools.vnext_spike.decision_packet as decision_packet
from tools.vnext_spike.decision_packet import (
    DecisionSkeletonError,
    build_analysis_draft,
    build_decision_packet,
    write_decision_packet,
)

from baibai_loop.foundation.yaml_io import safe_load

FIXTURE_ROOT = Path(__file__).parent / "fixtures/vnext-walking-skeleton"
REPO_ROOT = Path(__file__).parents[1]
FIXED_NOW = datetime(2026, 7, 11, 13, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


def _fixture(name: str) -> dict[str, object]:
    raw = safe_load((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _input() -> dict[str, object]:
    return _fixture("input.yaml")


def _review() -> dict[str, object]:
    return _fixture("review.yaml")


def _decision() -> dict[str, object]:
    return _fixture("decision.yaml")


def _hash(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _review_for(payload: dict[str, object]) -> dict[str, object]:
    review = _review()
    candidate = payload["candidate"]
    assert isinstance(candidate, dict)
    if candidate.get("evaluation_mode") == "current_decision":
        review["reviewed_at"] = "2026-07-11T12:45:00+09:00"
    review["reviewed_packet_sha256"] = _hash(
        build_analysis_draft(payload, source_root=REPO_ROOT, now=FIXED_NOW)
    )
    return review


def _current_input() -> dict[str, object]:
    payload = _input()
    candidate = payload["candidate"]
    quote = payload["quote_snapshot"]
    sources = payload["sources"]
    assert isinstance(candidate, dict)
    assert isinstance(quote, dict)
    assert isinstance(sources, list)
    candidate["as_of"] = FIXED_NOW.date().isoformat()
    candidate["evaluation_mode"] = "current_decision"
    quote["assumed_at"] = "2026-07-11T12:50:00+09:00"
    quote["origin"] = "observed"
    quote["freshness_status"] = "current"
    quote["is_executable"] = True
    quote_source = next(source for source in sources if source["source_id"] == "synthetic-quote")
    quote_source["as_of"] = FIXED_NOW.date().isoformat()
    quote_source["source_kind"] = "observed_market_quote"
    quote_source["source_tier"] = "secondary"
    return payload


def test_walking_skeleton_writes_deterministic_packet(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    write_decision_packet(
        FIXTURE_ROOT / "input.yaml",
        FIXTURE_ROOT / "review.yaml",
        first_path,
        decision_path=FIXTURE_ROOT / "decision.yaml",
        source_root=REPO_ROOT,
    )

    write_decision_packet(
        FIXTURE_ROOT / "input.yaml",
        FIXTURE_ROOT / "review.yaml",
        second_path,
        decision_path=FIXTURE_ROOT / "decision.yaml",
        source_root=REPO_ROOT,
    )

    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_path.read_bytes() == (FIXTURE_ROOT / "expected.json").read_bytes()


def test_analysis_review_and_human_outcome_are_separate_and_bound() -> None:
    payload = _input()
    draft = build_analysis_draft(payload, source_root=REPO_ROOT)
    assert "recorded_outcome" not in draft
    assert "independent_review" not in draft["detail"]  # type: ignore[operator]

    packet = build_decision_packet(payload, _review(), _decision(), source_root=REPO_ROOT)

    summary = packet["summary"]
    assert isinstance(summary, dict)
    assert summary["packet_status"] == "historical_replay_only"
    assert summary["is_actionable"] is False
    assert summary["ai_recommendation"] == "defer_non_executable_replay"
    assert packet["historical_outcome"] == {
        "decision": "approve",
        "decision_mode": "historical_record",
        "decided_at": "2026-07-03T12:00:00+09:00",
        "reference": "decision-20260703-2331-trade",
        "proposal_sha256": packet["proposal_sha256"],
        "analysis_draft_sha256": packet["analysis_draft_sha256"],
    }
    assert packet["user_decision"] == {"decision": "pending"}


def test_walking_skeleton_surfaces_price_and_evidence_constraints() -> None:
    packet = build_decision_packet(_input(), _review(), source_root=REPO_ROOT)

    summary = packet["summary"]
    detail = packet["detail"]
    assert isinstance(summary, dict)
    assert isinstance(detail, dict)
    price_policy = summary["price_policy"]
    portfolio_impact = summary["portfolio_impact"]
    quote = summary["quote"]
    assert isinstance(price_policy, dict)
    assert isinstance(portfolio_impact, dict)
    assert isinstance(quote, dict)
    assert price_policy["tactic"] == "shallow_limit"
    assert price_policy["max_acceptable_price_yen"] == 1050.0
    assert portfolio_impact["available_cash_if_executed_yen"] == 8_128_900.0
    assert portfolio_impact["existing_reservations_yen"] == 119_000.0
    assert quote["origin"] == "synthetic_fixture"
    assert quote["is_executable"] is False
    assert len(detail["evidence_gaps"]) == 4


def test_missing_permanent_loss_axis_returns_incomplete_packet() -> None:
    payload = _input()
    risks = payload["permanent_loss_risks"]
    assert isinstance(risks, list)
    missing = risks.pop()["axis"]

    packet = build_decision_packet(
        payload, _review_for(payload), source_root=REPO_ROOT, now=FIXED_NOW
    )

    summary = packet["summary"]
    detail = packet["detail"]
    assert isinstance(summary, dict)
    assert isinstance(detail, dict)
    assert summary["packet_status"] == "incomplete"
    assert detail["completeness_errors"] == [f"missing permanent-loss risk axis: {missing}"]


def test_review_must_target_the_exact_immutable_draft() -> None:
    payload = _input()
    proposal = payload["proposal"]
    assert isinstance(proposal, dict)
    proposal["quantity"] = 200

    with pytest.raises(DecisionSkeletonError, match="reviewed_packet_sha256 mismatch"):
        build_decision_packet(payload, _review(), source_root=REPO_ROOT)


def test_user_decision_is_invalidated_when_proposal_changes() -> None:
    payload = _input()
    proposal = payload["proposal"]
    assert isinstance(proposal, dict)
    proposal["quantity"] = 200

    with pytest.raises(DecisionSkeletonError, match="does not match proposal_sha256"):
        build_decision_packet(payload, _review_for(payload), _decision(), source_root=REPO_ROOT)


def test_independent_review_detects_arithmetic_mismatch() -> None:
    review = _review()
    rows = review["recalculated_scenario_cagrs"]
    assert isinstance(rows, list)
    rows[0]["total_return_cagr_pct"] = 99.0

    packet = build_decision_packet(_input(), review, source_root=REPO_ROOT)

    detail = packet["detail"]
    assert isinstance(detail, dict)
    independent = detail["independent_review"]
    assert independent["arithmetic_check"] == "unverified"
    assert independent["arithmetic_mismatches"]


def test_independent_review_recomputes_the_packet_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = build_analysis_draft(_input(), source_root=REPO_ROOT)
    detail = draft["detail"]
    assert isinstance(detail, dict)
    scenarios = detail["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["total_return_cagr_pct"] = 99.0
    review = _review()
    review["reviewed_packet_sha256"] = _hash(draft)
    monkeypatch.setattr(
        decision_packet,
        "build_analysis_draft",
        lambda *_args, **_kwargs: copy.deepcopy(draft),
    )

    packet = decision_packet.build_decision_packet(_input(), review, source_root=REPO_ROOT)

    packet_detail = packet["detail"]
    assert isinstance(packet_detail, dict)
    independent = packet_detail["independent_review"]
    assert independent["arithmetic_check"] == "unverified"
    assert any(
        "does not match independent recomputation" in mismatch
        for mismatch in independent["arithmetic_mismatches"]
    )


def test_review_proposal_change_requires_a_new_draft() -> None:
    review = _review()
    review["proposal_changed"] = True
    review["proposed_patch"] = "Lower max_acceptable_price_yen to 1000."

    packet = build_decision_packet(_input(), review, source_root=REPO_ROOT)

    summary = packet["summary"]
    assert isinstance(summary, dict)
    assert summary["packet_status"] == "revision_required"
    assert summary["is_actionable"] is False


def test_primary_verification_requires_a_checked_primary_source() -> None:
    review = _review()
    review["primary_source_check"] = "verified"
    review["checked_source_ids"] = ["synthetic-quote"]

    with pytest.raises(DecisionSkeletonError, match="requires a primary source"):
        build_decision_packet(_input(), review, source_root=REPO_ROOT)


def test_risk_cannot_pass_with_missing_evidence() -> None:
    payload = _input()
    risks = payload["permanent_loss_risks"]
    assert isinstance(risks, list)
    risk = risks[0]
    assert isinstance(risk, dict)
    risk["assessment"] = "pass"
    risk["evidence_status"] = "missing"

    with pytest.raises(DecisionSkeletonError, match="cannot pass with missing evidence"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_current_packet_can_reach_human_decision_with_warnings() -> None:
    payload = _current_input()

    packet = build_decision_packet(
        payload, _review_for(payload), source_root=REPO_ROOT, now=FIXED_NOW
    )

    summary = packet["summary"]
    assert isinstance(summary, dict)
    assert summary["packet_status"] == "ready_for_user_decision_with_warnings"
    assert summary["decision_readiness"] == "ready_with_warnings"
    assert summary["is_actionable"] is True
    assert summary["ai_recommendation"] == "shallow_limit"


def test_current_decision_is_bound_to_independent_review() -> None:
    payload = _current_input()
    review = _review_for(payload)
    packet = build_decision_packet(payload, review, source_root=REPO_ROOT, now=FIXED_NOW)
    summary = packet["summary"]
    detail = packet["detail"]
    assert isinstance(summary, dict)
    assert isinstance(detail, dict)
    independent = detail["independent_review"]
    assert isinstance(independent, dict)
    decision = {
        "decision": "approve",
        "decision_mode": "current_decision",
        "decided_at": "2026-07-11T12:55:00+09:00",
        "reference": "manual-current-decision",
        "proposal_sha256": packet["proposal_sha256"],
        "analysis_draft_sha256": packet["analysis_draft_sha256"],
        "review_id": independent["review_id"],
        "review_sha256": packet["independent_review_sha256"],
        "acknowledged_warning_ids": summary["warning_ids"],
        "warning_acceptance_reason": "Accepted after reviewing the listed uncertainty.",
    }

    accepted = build_decision_packet(
        payload, review, decision, source_root=REPO_ROOT, now=FIXED_NOW
    )
    user_decision = accepted["user_decision"]
    assert isinstance(user_decision, dict)
    assert user_decision["decision"] == "approve"
    assert user_decision["acknowledged_warning_ids"] == summary["warning_ids"]
    assert user_decision["warning_acceptance_reason"] == (
        "Accepted after reviewing the listed uncertainty."
    )

    changed_review = copy.deepcopy(review)
    changed_review["strongest_countercase"] = "A changed countercase changes the review digest."
    with pytest.raises(DecisionSkeletonError, match="does not match review_sha256"):
        build_decision_packet(
            payload, changed_review, decision, source_root=REPO_ROOT, now=FIXED_NOW
        )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_financial_values_are_rejected(number: float) -> None:
    payload = _input()
    quote = payload["quote_snapshot"]
    assert isinstance(quote, dict)
    quote["last_yen"] = number

    with pytest.raises(DecisionSkeletonError, match="finite number"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_tick_size_cannot_create_zero_price() -> None:
    payload = _input()
    proposal = payload["proposal"]
    assert isinstance(proposal, dict)
    proposal["tick_size_yen"] = 2000

    with pytest.raises(DecisionSkeletonError, match="tick_size_yen"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


@pytest.mark.parametrize(
    ("collection", "key"),
    [("scenarios", "scenario"), ("load_bearing_claims", "claim_id")],
)
def test_duplicate_decision_evidence_is_rejected(collection: str, key: str) -> None:
    payload = _input()
    rows = payload[collection]
    assert isinstance(rows, list)
    rows.append(copy.deepcopy(rows[0]))

    with pytest.raises(DecisionSkeletonError, match=f"duplicate {key}"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_unknown_or_missing_source_reference_is_rejected() -> None:
    payload = _input()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["source_ids"] = ["not-registered"]

    with pytest.raises(DecisionSkeletonError, match="unknown source ids"):
        build_analysis_draft(payload, source_root=REPO_ROOT)

    payload = _input()
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["ref"] = "records/does-not-exist.md"
    with pytest.raises(DecisionSkeletonError, match="source ref does not exist"):
        build_analysis_draft(payload, source_root=REPO_ROOT)

    payload = _input()
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["ref"] = "/etc/hosts"
    with pytest.raises(DecisionSkeletonError, match="escapes repository root"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_source_cannot_postdate_the_candidate_snapshot() -> None:
    payload = _input()
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["as_of"] = "2026-07-04"

    with pytest.raises(DecisionSkeletonError, match="is after candidate"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_historical_quote_cannot_be_marked_executable() -> None:
    payload = _input()
    quote = payload["quote_snapshot"]
    assert isinstance(quote, dict)
    quote["is_executable"] = True

    with pytest.raises(DecisionSkeletonError, match="non-executable historical quote"):
        build_analysis_draft(payload, source_root=REPO_ROOT)


def test_current_decision_requires_an_observed_quote_from_today() -> None:
    payload = _current_input()
    quote = payload["quote_snapshot"]
    assert isinstance(quote, dict)

    packet = build_decision_packet(
        payload, _review_for(payload), source_root=REPO_ROOT, now=FIXED_NOW
    )

    summary = packet["summary"]
    assert isinstance(summary, dict)
    assert summary["packet_status"] == "ready_for_user_decision_with_warnings"
    assert summary["execution_readiness"] == "executable_quote_available"

    quote["origin"] = "synthetic_fixture"
    with pytest.raises(DecisionSkeletonError, match="executable current-date quote"):
        build_analysis_draft(payload, source_root=REPO_ROOT, now=FIXED_NOW)
