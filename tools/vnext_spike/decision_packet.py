"""Disposable fixture runner for the vNext decision-packet walking skeleton."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from baibai_loop.foundation.yaml_io import safe_load

_SCENARIO_NAMES = frozenset({"bear", "base", "bull"})
_SCENARIO_HORIZONS = frozenset({3, 5})
_RISK_AXES = frozenset(
    {
        "balance_sheet_liquidity",
        "market_liquidity",
        "debt_repayment",
        "cash_flow_conversion",
        "dilution",
        "customer_concentration",
        "structural_decline",
        "governance_accounting",
    }
)
_RISK_ASSESSMENTS = frozenset({"pass", "concern", "unknown"})
_EVIDENCE_STATUSES = frozenset({"verified", "partial", "missing"})
_EVALUATION_MODES = frozenset({"historical_replay", "current_decision"})
_QUOTE_FRESHNESS = frozenset({"current", "stale", "historical"})
_REVIEW_CHECKS = frozenset({"verified", "partially_verified", "unverified"})
_ALTERNATIVE_CHECKS = frozenset({"compared", "unavailable"})
_DECISIONS = frozenset({"approve", "defer", "reject"})


class DecisionSkeletonError(ValueError):
    """Raised when a fixture violates a walking-skeleton invariant."""


def build_analysis_draft(
    payload: Mapping[str, object],
    *,
    source_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build an immutable pre-decision packet from offline analysis inputs."""

    candidate = _mapping(payload, "candidate")
    portfolio = _mapping(payload, "portfolio_snapshot")
    quote = _mapping(payload, "quote_snapshot")
    proposal = _mapping(payload, "proposal")
    greatest_risk = _mapping(payload, "greatest_risk")
    estimate_input = _mapping(payload, "estimate_snapshot")
    policy_input = _mapping(payload, "policy_snapshot")

    ticker = _string(candidate, "ticker")
    name = _string(candidate, "name")
    as_of = _iso_date(candidate, "as_of")
    evaluation_mode = _enum(candidate, "evaluation_mode", _EVALUATION_MODES)

    sources = _sources(payload.get("sources"), source_root=source_root, max_as_of=as_of)
    source_ids = frozenset(source["source_id"] for source in sources)
    source_tiers = {source["source_id"]: source["source_tier"] for source in sources}
    proposal_source_ids = _validated_source_ids(
        proposal.get("source_ids"), field="proposal.source_ids", source_ids=source_ids
    )
    portfolio_source_ids = _validated_source_ids(
        portfolio.get("source_ids"),
        field="portfolio_snapshot.source_ids",
        source_ids=source_ids,
    )

    max_price = _positive_number(proposal, "max_acceptable_price_yen")
    quantity = _positive_int(proposal, "quantity")
    board_lot = _positive_int(proposal, "board_lot")
    if quantity % board_lot:
        raise DecisionSkeletonError("proposal.quantity must be a multiple of board_lot")

    total_capital = _nonnegative_number(portfolio, "total_capital_yen")
    cash_balance = _nonnegative_number(portfolio, "cash_balance_yen")
    existing_reservations = _nonnegative_number(portfolio, "existing_reservations_yen")
    dry_powder_floor = _nonnegative_number(portfolio, "dry_powder_floor_yen")
    if cash_balance > total_capital:
        raise DecisionSkeletonError("cash balance cannot exceed total capital")
    if existing_reservations > cash_balance:
        raise DecisionSkeletonError("existing reservations cannot exceed cash balance")
    available_cash = cash_balance - existing_reservations
    if dry_powder_floor > available_cash:
        raise DecisionSkeletonError("dry-powder floor cannot exceed available cash")

    quote_snapshot = _quote_snapshot(
        quote,
        source_ids=source_ids,
        source_tiers=source_tiers,
        evaluation_mode=evaluation_mode,
        candidate_as_of=as_of,
        now=now or datetime.now(tz=ZoneInfo("Asia/Tokyo")),
    )
    price_plan = _price_plan(
        quote_snapshot,
        max_price=max_price,
        quantity=quantity,
        available_cash=available_cash,
        dry_powder_floor=dry_powder_floor,
        tick_size=_positive_number(proposal, "tick_size_yen"),
        deep_discount_pct=_bounded_fraction(proposal, "deep_discount_pct"),
    )
    scenarios = _scenarios(payload.get("scenarios"), entry_price=max_price, source_ids=source_ids)
    risks, risk_errors = _risk_axes(payload.get("permanent_loss_risks"), source_ids=source_ids)
    claims = _claims(payload.get("load_bearing_claims"), source_ids=source_ids)
    evidence_gaps = _string_list(payload.get("evidence_gaps"), field="evidence_gaps")

    estimate = {
        "legacy_fair_value_yen": _positive_number(estimate_input, "legacy_fair_value_yen"),
        "legacy_expected_yield_pct": _finite_number(estimate_input, "legacy_expected_yield_pct"),
        "legacy_risk_reward_ratio": _positive_number(estimate_input, "legacy_risk_reward_ratio"),
        "five_year_base_total_return_cagr_pct": _scenario_cagr(scenarios, horizon=5, name="base"),
        "three_year_sanity_total_return_cagr_pct": _scenario_cagr(
            scenarios, horizon=3, name="base"
        ),
        "valuation_method": _string(estimate_input, "valuation_method"),
        "origin": "estimate",
        "source_ids": _validated_source_ids(
            estimate_input.get("source_ids"),
            field="estimate_snapshot.source_ids",
            source_ids=source_ids,
        ),
        "derivation": _string(estimate_input, "derivation"),
    }
    risk_statement = {
        "statement": _string(greatest_risk, "statement"),
        "origin": "judgment",
        "source_ids": _validated_source_ids(
            greatest_risk.get("source_ids"),
            field="greatest_risk.source_ids",
            source_ids=source_ids,
        ),
    }

    recommended_price = price_plan["recommended_price_yen"]
    assert isinstance(recommended_price, float)
    prospective_notional = recommended_price * quantity
    available_after = available_cash - prospective_notional
    proposal_target = {
        "ticker": ticker,
        "as_of": as_of,
        "max_acceptable_price_yen": max_price,
        "quantity": quantity,
        "side": "buy",
    }
    proposal_sha256 = _packet_hash(proposal_target)
    completeness_errors = risk_errors
    initial_status = "incomplete" if completeness_errors else "awaiting_independent_review"
    draft: dict[str, object] = {
        "schema_version": "vnext-walking-skeleton-1",
        "proposal_sha256": proposal_sha256,
        "summary": {
            "candidate": {"ticker": ticker, "name": name},
            "as_of": as_of,
            "evaluation_mode": evaluation_mode,
            "packet_status": initial_status,
            "is_actionable": False,
            "ai_recommendation": "defer_pending_independent_review",
            "investment_case": estimate,
            "permanent_loss_conclusion": _risk_conclusion(risks, risk_errors),
            "confidence": {
                "level": "low" if evidence_gaps or risk_errors else "medium",
                "reason": (
                    "Material evidence or completeness gaps remain."
                    if evidence_gaps or risk_errors
                    else "No material gap is recorded in the fixture."
                ),
            },
            "price_policy": {
                "tactic": price_plan["tactic"],
                "max_acceptable_price_yen": max_price,
                "quantity": quantity,
                "board_lot": board_lot,
                "origin": "judgment",
                "source_ids": proposal_source_ids,
                "derivation": "Never exceed the thesis price ceiling or dry-powder floor.",
            },
            "quote": quote_snapshot,
            "price_options": price_plan["options"],
            "portfolio_impact": {
                "total_capital_yen": total_capital,
                "cash_balance_yen": cash_balance,
                "existing_reservations_yen": existing_reservations,
                "available_cash_before_yen": available_cash,
                "prospective_notional_yen": prospective_notional,
                "available_cash_if_executed_yen": available_after,
                "dry_powder_floor_yen": dry_powder_floor,
                "dry_powder_headroom_yen": available_after - dry_powder_floor,
                "origin": "derived",
                "source_ids": portfolio_source_ids,
                "derivation": (
                    "available=cash_balance-existing_reservations; "
                    "after=available-prospective_notional"
                ),
            },
            "policy_checks": _policy_checks(
                policy_input,
                source_ids=source_ids,
                total_capital=total_capital,
                prospective_notional=prospective_notional,
            ),
            "greatest_risk": risk_statement,
        },
        "detail": {
            "scenarios": scenarios,
            "permanent_loss_risks": risks,
            "load_bearing_claims": claims,
            "sources": sources,
            "evidence_gaps": evidence_gaps,
            "completeness_errors": completeness_errors,
        },
    }
    return draft


def build_decision_packet(
    payload: Mapping[str, object],
    review_payload: Mapping[str, object],
    decision_payload: Mapping[str, object] | None = None,
    *,
    source_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Attach an independent review and optional bound human outcome to a draft."""

    evaluated_at = now or datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    draft = build_analysis_draft(payload, source_root=source_root, now=evaluated_at)
    draft_hash = _packet_hash(draft)
    detail = draft["detail"]
    summary = draft["summary"]
    assert isinstance(detail, dict)
    assert isinstance(summary, dict)
    scenarios = detail["scenarios"]
    sources = detail["sources"]
    completeness_errors = detail["completeness_errors"]
    evidence_gaps = detail["evidence_gaps"]
    risks = detail["permanent_loss_risks"]
    assert isinstance(scenarios, list)
    assert isinstance(sources, list)
    assert isinstance(completeness_errors, list)
    assert isinstance(evidence_gaps, list)
    assert isinstance(risks, list)
    source_tiers = {str(source["source_id"]): str(source["source_tier"]) for source in sources}
    source_ids = frozenset(source_tiers)
    price_policy = summary["price_policy"]
    assert isinstance(price_policy, dict)
    review = _independent_review(
        review_payload,
        expected_hash=draft_hash,
        source_ids=source_ids,
        source_tiers=source_tiers,
        scenarios=scenarios,
        entry_price=float(price_policy["max_acceptable_price_yen"]),
    )
    detail["independent_review"] = review
    draft["analysis_draft_sha256"] = draft_hash

    warning_ids = [f"evidence_gap:{index}" for index, _ in enumerate(evidence_gaps, start=1)]
    warning_ids.extend(
        f"risk:{risk['axis']}"
        for risk in risks
        if isinstance(risk, Mapping) and risk.get("assessment") != "pass"
    )
    warning_ids.extend(
        f"risk_evidence:{risk['axis']}"
        for risk in risks
        if isinstance(risk, Mapping) and risk.get("evidence_status") != "verified"
    )
    policy_checks = summary["policy_checks"]
    assert isinstance(policy_checks, Mapping)
    policy_warnings = policy_checks["warnings"]
    assert isinstance(policy_warnings, list)
    warning_ids.extend(f"policy:{warning}" for warning in policy_warnings)
    if review["primary_source_check"] != "verified":
        warning_ids.append("review:primary_sources_incomplete")
    if review["alternative_candidate_check"] != "compared":
        warning_ids.append("review:alternative_candidate_unavailable")
    if completeness_errors:
        packet_status = "incomplete"
    elif review["arithmetic_check"] != "verified":
        packet_status = "review_required"
    elif review["proposal_changed"]:
        packet_status = "revision_required"
    elif summary["evaluation_mode"] == "historical_replay":
        packet_status = "historical_replay_only"
    elif warning_ids:
        packet_status = "ready_for_user_decision_with_warnings"
    else:
        packet_status = "ready_for_user_decision"
    summary["packet_status"] = packet_status
    summary["warning_ids"] = warning_ids
    summary["decision_readiness"] = (
        "historical_reference"
        if packet_status == "historical_replay_only"
        else (
            "ready_with_warnings"
            if packet_status == "ready_for_user_decision_with_warnings"
            else ("ready" if packet_status == "ready_for_user_decision" else "not_ready")
        )
    )
    summary["is_actionable"] = packet_status in {
        "ready_for_user_decision",
        "ready_for_user_decision_with_warnings",
    }
    if packet_status == "historical_replay_only":
        summary["ai_recommendation"] = "defer_non_executable_replay"
    elif summary["is_actionable"]:
        summary["ai_recommendation"] = str(price_policy["tactic"])
    else:
        summary["ai_recommendation"] = "defer_pending_review"
    if summary["evaluation_mode"] == "historical_replay":
        summary["execution_readiness"] = "non_executable_historical"
    elif summary["is_actionable"] and price_policy["tactic"] != "defer":
        summary["execution_readiness"] = "executable_quote_available"
    else:
        summary["execution_readiness"] = "not_executable"
    review_sha256 = _packet_hash(review)
    draft["independent_review_sha256"] = review_sha256
    outcome = _user_decision(
        decision_payload,
        proposal_sha256=str(draft["proposal_sha256"]),
        analysis_draft_sha256=draft_hash,
        evaluation_mode=str(summary["evaluation_mode"]),
        packet_status=packet_status,
        warning_ids=warning_ids,
        review_id=str(review["review_id"]),
        review_sha256=review_sha256,
        reviewed_at=str(review["reviewed_at"]),
        now=evaluated_at,
    )
    if outcome.get("decision_mode") == "historical_record":
        draft["historical_outcome"] = outcome
        draft["user_decision"] = {"decision": "pending"}
    else:
        draft["user_decision"] = outcome
    return draft


def write_decision_packet(
    input_path: Path,
    review_path: Path,
    output_path: Path,
    *,
    decision_path: Path | None = None,
    source_root: Path | None = None,
) -> None:
    """Read separated offline artifacts and write strict deterministic JSON."""

    payload = _load_mapping(input_path, label="input")
    review = _load_mapping(review_path, label="review")
    decision = _load_mapping(decision_path, label="decision") if decision_path else None
    packet = build_decision_packet(payload, review, decision, source_root=source_root or Path.cwd())
    output_path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _quote_snapshot(
    value: Mapping[str, object],
    *,
    source_ids: frozenset[str],
    source_tiers: Mapping[str, str],
    evaluation_mode: str,
    candidate_as_of: str,
    now: datetime,
) -> dict[str, object]:
    bid = _positive_number(value, "bid_yen")
    ask = _positive_number(value, "ask_yen")
    if bid > ask:
        raise DecisionSkeletonError("quote bid_yen cannot exceed ask_yen")
    assumed_at = _iso_datetime(value, "assumed_at")
    freshness = _enum(value, "freshness_status", _QUOTE_FRESHNESS)
    executable = _boolean(value, "is_executable")
    origin = _enum(value, "origin", frozenset({"observed", "synthetic_fixture"}))
    assumed_datetime = datetime.fromisoformat(assumed_at)
    if evaluation_mode == "historical_replay" and (freshness != "historical" or executable):
        raise DecisionSkeletonError("historical replay requires a non-executable historical quote")
    if evaluation_mode == "current_decision" and (
        freshness != "current"
        or not executable
        or origin != "observed"
        or assumed_at[:10] != candidate_as_of
        or candidate_as_of != now.date().isoformat()
        or not timedelta() <= now - assumed_datetime <= timedelta(minutes=15)
    ):
        raise DecisionSkeletonError("current decision requires an executable current-date quote")
    source_id = _validated_source_id(
        value, "source_id", field="quote_snapshot.source_id", source_ids=source_ids
    )
    if evaluation_mode == "current_decision" and source_tiers[source_id] == "synthetic_fixture":
        raise DecisionSkeletonError("current decision requires a non-synthetic quote source")
    return {
        "last_yen": _positive_number(value, "last_yen"),
        "bid_yen": bid,
        "ask_yen": ask,
        "assumed_at": assumed_at,
        "source_id": source_id,
        "origin": origin,
        "freshness_status": freshness,
        "is_executable": executable,
    }


def _price_plan(
    quote: Mapping[str, object],
    *,
    max_price: float,
    quantity: int,
    available_cash: float,
    dry_powder_floor: float,
    tick_size: float,
    deep_discount_pct: float,
) -> dict[str, object]:
    bid = cast(float, quote["bid_yen"])
    ask = cast(float, quote["ask_yen"])
    if tick_size > min(bid, max_price):
        raise DecisionSkeletonError("tick_size_yen cannot exceed the candidate order price")
    shallow = _floor_to_tick(min(bid, max_price), tick_size)
    deep = min(shallow, _floor_to_tick(max_price * (1 - deep_discount_pct), tick_size))
    if shallow <= 0 or deep <= 0:
        raise DecisionSkeletonError("rounded order prices must remain positive")
    buy_now_allowed = ask <= max_price and available_cash - ask * quantity >= dry_powder_floor
    shallow_allowed = available_cash - shallow * quantity >= dry_powder_floor
    deep_allowed = available_cash - deep * quantity >= dry_powder_floor
    if buy_now_allowed:
        tactic = "buy_now"
        recommended_price = ask
    elif shallow_allowed:
        tactic = "shallow_limit"
        recommended_price = shallow
    else:
        tactic = "defer"
        recommended_price = 0.0
    return {
        "tactic": tactic,
        "recommended_price_yen": recommended_price,
        "options": [
            {
                "action": "buy_now",
                "price_yen": ask,
                "price_eligible": buy_now_allowed,
                "reason": "ask_within_max_price" if ask <= max_price else "ask_above_max_price",
            },
            {
                "action": "shallow_limit",
                "price_yen": shallow,
                "price_eligible": shallow_allowed,
                "reason": "best_bid_capped_by_max_price",
            },
            {
                "action": "deep_limit",
                "price_yen": deep,
                "price_eligible": deep_allowed,
                "reason": "price_priority_with_lower_fill_likelihood",
            },
            {
                "action": "defer",
                "price_yen": None,
                "price_eligible": True,
                "reason": "preserve_cash",
            },
        ],
    }


def _scenarios(
    value: object, *, entry_price: float, source_ids: frozenset[str]
) -> list[dict[str, object]]:
    rows = _mapping_list(value, field="scenarios")
    seen: set[tuple[int, str]] = set()
    output: list[dict[str, object]] = []
    for row in rows:
        horizon = _positive_int(row, "horizon_years")
        name = _string(row, "name")
        key = (horizon, name)
        if horizon not in _SCENARIO_HORIZONS or name not in _SCENARIO_NAMES:
            raise DecisionSkeletonError("scenarios must contain bear/base/bull for 3y and 5y")
        if key in seen:
            raise DecisionSkeletonError(f"duplicate scenario: {horizon}y/{name}")
        seen.add(key)
        terminal_price = _nonnegative_number(row, "terminal_price_yen")
        dividends = _nonnegative_number(row, "total_dividends_yen")
        total_value = terminal_price + dividends
        cagr = (total_value / entry_price) ** (1 / horizon) - 1
        output.append(
            {
                "horizon_years": horizon,
                "name": name,
                "terminal_price_yen": terminal_price,
                "total_dividends_yen": dividends,
                "total_return_cagr_pct": round(cagr * 100, 2),
                "assumption": _string(row, "assumption"),
                "origin": "estimate",
                "source_ids": _validated_source_ids(
                    row.get("source_ids"), field="scenarios.source_ids", source_ids=source_ids
                ),
                "derivation": _string(row, "derivation"),
            }
        )
    expected = {(horizon, name) for horizon in _SCENARIO_HORIZONS for name in _SCENARIO_NAMES}
    if seen != expected:
        raise DecisionSkeletonError("scenarios must contain each bear/base/bull 3y/5y pair once")
    for horizon in _SCENARIO_HORIZONS:
        values = {
            cast(str, row["name"]): cast(float, row["terminal_price_yen"])
            + cast(float, row["total_dividends_yen"])
            for row in output
            if row["horizon_years"] == horizon
        }
        if not values["bear"] <= values["base"] <= values["bull"]:
            raise DecisionSkeletonError(f"scenario total values are not ordered for {horizon}y")
    return sorted(
        output,
        key=lambda row: (cast(int, row["horizon_years"]), cast(str, row["name"])),
    )


def _risk_axes(
    value: object, *, source_ids: frozenset[str]
) -> tuple[list[dict[str, str]], list[str]]:
    rows = _mapping_list(value, field="permanent_loss_risks")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        axis = _string(row, "axis")
        if axis not in _RISK_AXES:
            raise DecisionSkeletonError(f"invalid permanent-loss risk axis: {axis}")
        if axis in seen:
            raise DecisionSkeletonError(f"duplicate permanent-loss risk axis: {axis}")
        seen.add(axis)
        assessment = _enum(row, "assessment", _RISK_ASSESSMENTS)
        evidence_status = _enum(row, "evidence_status", _EVIDENCE_STATUSES)
        if assessment == "pass" and evidence_status == "missing":
            raise DecisionSkeletonError(f"risk axis {axis} cannot pass with missing evidence")
        output.append(
            {
                "axis": axis,
                "assessment": assessment,
                "evidence_status": evidence_status,
                "summary": _string(row, "summary"),
                "source_id": _validated_source_id(
                    row, "source_id", field="risk.source_id", source_ids=source_ids
                ),
            }
        )
    errors = [f"missing permanent-loss risk axis: {axis}" for axis in sorted(_RISK_AXES - seen)]
    return sorted(output, key=lambda row: row["axis"]), errors


def _risk_conclusion(
    risks: list[dict[str, str]], completeness_errors: list[str]
) -> dict[str, object]:
    if completeness_errors:
        conclusion = "incomplete"
    elif any(risk["assessment"] == "concern" for risk in risks):
        conclusion = "concern"
    elif any(risk["assessment"] == "unknown" for risk in risks):
        conclusion = "unknown"
    else:
        conclusion = "pass"
    return {
        "conclusion": conclusion,
        "origin": "derived",
        "risk_axis_ids": [risk["axis"] for risk in risks],
        "derivation": "Worst explicit axis assessment; missing axes force incomplete.",
    }


def _policy_checks(
    value: Mapping[str, object],
    *,
    source_ids: frozenset[str],
    total_capital: float,
    prospective_notional: float,
) -> dict[str, object]:
    actuals = {
        "ticker_weight": (
            _nonnegative_number(value, "existing_ticker_notional_yen") + prospective_notional
        )
        / total_capital
        * 100,
        "sector_weight": (
            _nonnegative_number(value, "existing_sector_notional_yen") + prospective_notional
        )
        / total_capital
        * 100,
        "playbook_weight": (
            _nonnegative_number(value, "existing_playbook_notional_yen") + prospective_notional
        )
        / total_capital
        * 100,
        "adv_participation": prospective_notional
        / _positive_number(value, "average_daily_value_yen")
        * 100,
    }
    checks: list[dict[str, object]] = []
    for name in ("ticker_weight", "sector_weight", "playbook_weight", "adv_participation"):
        actual = round(actuals[name], 4)
        warning = _positive_number(value, f"{name}_warning_pct")
        checks.append(
            {
                "name": name,
                "actual_pct": actual,
                "warning_pct": warning,
                "status": "warning" if actual > warning else "within_warning_line",
            }
        )
    return {
        "checks": checks,
        "warnings": [check["name"] for check in checks if check["status"] == "warning"],
        "origin": "derived",
        "source_ids": _validated_source_ids(
            value.get("source_ids"), field="policy_snapshot.source_ids", source_ids=source_ids
        ),
        "derivation": (
            "Post-order ticker, sector, and playbook notionals use the proposal notional; "
            "ADV participation is proposal notional divided by average daily value."
        ),
    }


def _sources(value: object, *, source_root: Path | None, max_as_of: str) -> list[dict[str, str]]:
    rows = _mapping_list(value, field="sources")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        source_id = _string(row, "source_id")
        if source_id in seen:
            raise DecisionSkeletonError(f"duplicate source_id: {source_id}")
        seen.add(source_id)
        ref = _string(row, "ref")
        if source_root is not None and not ref.startswith(("https://", "http://")):
            root = source_root.resolve()
            resolved = (root / ref).resolve()
            if not resolved.is_relative_to(root):
                raise DecisionSkeletonError(f"source ref escapes repository root: {ref}")
            if not resolved.is_file():
                raise DecisionSkeletonError(f"source ref does not exist: {ref}")
        source_as_of = _iso_date(row, "as_of")
        if source_as_of > max_as_of:
            raise DecisionSkeletonError(
                f"source {source_id} as_of {source_as_of} is after candidate {max_as_of}"
            )
        output.append(
            {
                "source_id": source_id,
                "ref": ref,
                "as_of": source_as_of,
                "used_for": _string(row, "used_for"),
                "source_kind": _string(row, "source_kind"),
                "source_tier": _enum(
                    row,
                    "source_tier",
                    frozenset({"primary", "secondary", "internal_record", "synthetic_fixture"}),
                ),
            }
        )
    if not output:
        raise DecisionSkeletonError("sources must not be empty")
    return output


def _claims(value: object, *, source_ids: frozenset[str]) -> list[dict[str, object]]:
    rows = _mapping_list(value, field="load_bearing_claims")
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        claim_id = _string(row, "claim_id")
        if claim_id in seen:
            raise DecisionSkeletonError(f"duplicate claim_id: {claim_id}")
        seen.add(claim_id)
        output.append(
            {
                "claim_id": claim_id,
                "claim": _string(row, "claim"),
                "origin": _enum(
                    row, "origin", frozenset({"observed", "derived", "estimate", "judgment"})
                ),
                "source_ids": _validated_source_ids(
                    row.get("source_ids"),
                    field="load_bearing_claims.source_ids",
                    source_ids=source_ids,
                ),
            }
        )
    if not output:
        raise DecisionSkeletonError("load_bearing_claims must not be empty")
    return output


def _independent_review(
    value: Mapping[str, object],
    *,
    expected_hash: str,
    source_ids: frozenset[str],
    source_tiers: Mapping[str, str],
    scenarios: list[object],
    entry_price: float,
) -> dict[str, object]:
    reviewed_hash = _string(value, "reviewed_packet_sha256")
    if reviewed_hash != expected_hash:
        raise DecisionSkeletonError(f"reviewed_packet_sha256 mismatch: expected {expected_hash}")
    checked = _validated_source_ids(
        value.get("checked_source_ids"), field="checked_source_ids", source_ids=source_ids
    )
    primary_check = _enum(value, "primary_source_check", _REVIEW_CHECKS)
    if primary_check == "verified" and not any(
        source_tiers[source_id] == "primary" for source_id in checked
    ):
        raise DecisionSkeletonError("verified primary source check requires a primary source")
    recalculated_rows = _mapping_list(
        value.get("recalculated_scenario_cagrs"), field="recalculated_scenario_cagrs"
    )
    expected_cagrs = _recalculate_scenario_cagrs(scenarios, entry_price=entry_price)
    supplied_cagrs: dict[tuple[int, str], float] = {}
    for row in recalculated_rows:
        key = (_positive_int(row, "horizon_years"), _string(row, "name"))
        if key in supplied_cagrs:
            raise DecisionSkeletonError(f"duplicate reviewed scenario: {key}")
        supplied_cagrs[key] = _finite_number(row, "total_return_cagr_pct")
    mismatches = [
        f"{horizon}y/{name}: expected {expected}, got {supplied_cagrs.get((horizon, name))}"
        for (horizon, name), expected in expected_cagrs.items()
        if supplied_cagrs.get((horizon, name)) != expected
    ]
    for item in scenarios:
        if not isinstance(item, Mapping):
            raise DecisionSkeletonError("scenario output must be a mapping")
        key = (int(item["horizon_years"]), str(item["name"]))
        if float(item["total_return_cagr_pct"]) != expected_cagrs[key]:
            mismatches.append(
                f"packet {key[0]}y/{key[1]} CAGR does not match independent recomputation"
            )
    arithmetic_check = "verified" if not mismatches else "unverified"
    changed = _boolean(value, "proposal_changed")
    proposed_patch = value.get("proposed_patch")
    if changed and (not isinstance(proposed_patch, str) or not proposed_patch.strip()):
        raise DecisionSkeletonError("changed review requires proposed_patch")
    if not changed and proposed_patch is not None:
        raise DecisionSkeletonError("unchanged review cannot include proposed_patch")
    return {
        "review_id": _string(value, "review_id"),
        "reviewer_role": _string(value, "reviewer_role"),
        "reviewed_at": _iso_datetime(value, "reviewed_at"),
        "reviewed_packet_sha256": reviewed_hash,
        "arithmetic_check": arithmetic_check,
        "arithmetic_mismatches": mismatches,
        "primary_source_check": primary_check,
        "strongest_countercase": _string(value, "strongest_countercase"),
        "alternative_candidate_check": _enum(
            value, "alternative_candidate_check", _ALTERNATIVE_CHECKS
        ),
        "proposal_changed": changed,
        "proposed_patch": proposed_patch,
        "checked_source_ids": checked,
    }


def _recalculate_scenario_cagrs(
    scenarios: list[object], *, entry_price: float
) -> dict[tuple[int, str], float]:
    output: dict[tuple[int, str], float] = {}
    for item in scenarios:
        if not isinstance(item, Mapping):
            raise DecisionSkeletonError("scenario output must be a mapping")
        horizon = int(item["horizon_years"])
        name = str(item["name"])
        total_value = float(item["terminal_price_yen"]) + float(item["total_dividends_yen"])
        output[(horizon, name)] = round(((total_value / entry_price) ** (1 / horizon) - 1) * 100, 2)
    return output


def _user_decision(
    value: Mapping[str, object] | None,
    *,
    proposal_sha256: str,
    analysis_draft_sha256: str,
    evaluation_mode: str,
    packet_status: str,
    warning_ids: list[str],
    review_id: str,
    review_sha256: str,
    reviewed_at: str,
    now: datetime,
) -> dict[str, object]:
    if value is None:
        return {"decision": "pending"}
    decision = _enum(value, "decision", _DECISIONS)
    if _string(value, "proposal_sha256") != proposal_sha256:
        raise DecisionSkeletonError("user decision does not match proposal_sha256")
    if _string(value, "analysis_draft_sha256") != analysis_draft_sha256:
        raise DecisionSkeletonError("user decision does not match analysis_draft_sha256")
    decision_mode = _enum(
        value, "decision_mode", frozenset({"historical_record", "current_decision"})
    )
    if (evaluation_mode == "historical_replay") != (decision_mode == "historical_record"):
        raise DecisionSkeletonError("decision_mode must match evaluation_mode")
    decided_at = _iso_datetime(value, "decided_at")
    warning_acceptance: dict[str, object] = {}
    if decision_mode == "current_decision":
        if _string(value, "review_id") != review_id:
            raise DecisionSkeletonError("user decision does not match review_id")
        if _string(value, "review_sha256") != review_sha256:
            raise DecisionSkeletonError("user decision does not match review_sha256")
        if datetime.fromisoformat(decided_at) < datetime.fromisoformat(reviewed_at):
            raise DecisionSkeletonError("current decision cannot predate independent review")
        if datetime.fromisoformat(decided_at) > now + timedelta(minutes=1):
            raise DecisionSkeletonError("current decision cannot be future-dated")
        if decision == "approve" and packet_status not in {
            "ready_for_user_decision",
            "ready_for_user_decision_with_warnings",
        }:
            raise DecisionSkeletonError("cannot approve a packet that is not decision-ready")
        if decision == "approve" and warning_ids:
            acknowledged = _string_list(
                value.get("acknowledged_warning_ids"), field="acknowledged_warning_ids"
            )
            if set(acknowledged) != set(warning_ids):
                raise DecisionSkeletonError("approval must acknowledge every packet warning")
            warning_acceptance = {
                "acknowledged_warning_ids": acknowledged,
                "warning_acceptance_reason": _string(value, "warning_acceptance_reason"),
            }
    return {
        "decision": decision,
        "decision_mode": decision_mode,
        "decided_at": decided_at,
        "reference": _string(value, "reference"),
        "proposal_sha256": proposal_sha256,
        "analysis_draft_sha256": analysis_draft_sha256,
        **(
            {"review_id": review_id, "review_sha256": review_sha256}
            if decision_mode == "current_decision"
            else {}
        ),
        **warning_acceptance,
    }


def _scenario_cagr(scenarios: list[dict[str, object]], *, horizon: int, name: str) -> float:
    for scenario in scenarios:
        if scenario["horizon_years"] == horizon and scenario["name"] == name:
            return cast(float, scenario["total_return_cagr_pct"])
    raise DecisionSkeletonError(f"missing scenario: {horizon}y/{name}")


def _validated_source_ids(value: object, *, field: str, source_ids: frozenset[str]) -> list[str]:
    refs = _string_list(value, field=field)
    if not refs:
        raise DecisionSkeletonError(f"{field} must not be empty")
    duplicates = [item for item, count in Counter(refs).items() if count > 1]
    if duplicates:
        raise DecisionSkeletonError(f"{field} contains duplicate source ids: {duplicates}")
    unknown = sorted(set(refs) - source_ids)
    if unknown:
        raise DecisionSkeletonError(f"{field} contains unknown source ids: {unknown}")
    return refs


def _validated_source_id(
    value: Mapping[str, object], key: str, *, field: str, source_ids: frozenset[str]
) -> str:
    source_id = _string(value, key)
    if source_id not in source_ids:
        raise DecisionSkeletonError(f"{field} is unknown: {source_id}")
    return source_id


def _load_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise DecisionSkeletonError(f"{label} root must be a mapping")
    return raw


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise DecisionSkeletonError(f"{key} must be a mapping")
    return item


def _mapping_list(value: object, *, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise DecisionSkeletonError(f"{field} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise DecisionSkeletonError(f"{field} entries must be mappings")
    return [item for item in value if isinstance(item, Mapping)]


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise DecisionSkeletonError(f"{key} must be a non-empty string")
    return item


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise DecisionSkeletonError(f"{field} must be a list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise DecisionSkeletonError(f"{field} entries must be non-empty strings")
    return [item for item in value if isinstance(item, str)]


def _finite_number(value: Mapping[str, object], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise DecisionSkeletonError(f"{key} must be a finite number")
    number = float(item)
    if not math.isfinite(number):
        raise DecisionSkeletonError(f"{key} must be a finite number")
    return number


def _positive_number(value: Mapping[str, object], key: str) -> float:
    number = _finite_number(value, key)
    if number <= 0:
        raise DecisionSkeletonError(f"{key} must be a positive number")
    return number


def _nonnegative_number(value: Mapping[str, object], key: str) -> float:
    number = _finite_number(value, key)
    if number < 0:
        raise DecisionSkeletonError(f"{key} must be a non-negative number")
    return number


def _positive_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise DecisionSkeletonError(f"{key} must be a positive integer")
    return item


def _bounded_fraction(value: Mapping[str, object], key: str) -> float:
    item = _positive_number(value, key)
    if item >= 1:
        raise DecisionSkeletonError(f"{key} must be below 1")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise DecisionSkeletonError(f"{key} must be a boolean")
    return item


def _enum(value: Mapping[str, object], key: str, choices: frozenset[str]) -> str:
    item = _string(value, key)
    if item not in choices:
        raise DecisionSkeletonError(f"{key} must be one of {sorted(choices)}")
    return item


def _iso_date(value: Mapping[str, object], key: str) -> str:
    item = _string(value, key)
    try:
        date.fromisoformat(item)
    except ValueError as error:
        raise DecisionSkeletonError(f"{key} must be an ISO date") from error
    return item


def _iso_datetime(value: Mapping[str, object], key: str) -> str:
    item = _string(value, key)
    try:
        parsed = datetime.fromisoformat(item)
    except ValueError as error:
        raise DecisionSkeletonError(f"{key} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise DecisionSkeletonError(f"{key} must include a timezone")
    return item


def _floor_to_tick(value: float, tick_size: float) -> float:
    return round(math.floor(value / tick_size) * tick_size, 6)


def _packet_hash(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_decision_packet(
        args.input,
        args.review,
        args.output,
        decision_path=args.decision,
    )


if __name__ == "__main__":
    main()
