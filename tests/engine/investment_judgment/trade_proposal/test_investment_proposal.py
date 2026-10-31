from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposal,
    load_proposal_draft,
    order_readiness,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.policy import validate_proposal_policy
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelection,
    selection_sha256,
    selection_targets,
)


@pytest.fixture
def payload():
    fixture = Path(__file__).parents[3] / "fixtures/domain-assessment-proposal-synthetic.json"
    return json.loads(fixture.read_text())["investment_proposal"]


def _position():
    return {
        "ticker": "1234",
        "sector": "情報・通信業",
        "common_factors": ["domestic-demand"],
        "quantity": 100,
        "cost_yen": "90000",
        "quantity_basis_confirmed": True,
        "original_assessment_ref": None,
        "original_investment_reason": None,
        "original_reason_unavailable_reason": "旧当初理由は確認できない",
    }


def _holding_payload(payload, *, action="hold"):
    payload["request_scope"] = "review_positions"
    payload["research_scope"]["targets"][0]["purpose"] = "holding"
    payload["research_scope"]["outcomes"][0]["purpose"] = "holding"
    payload["inputs"]["positions"] = [_position()]
    item = payload["items"][0]
    for name in ("timing", "terms", "proposed_limit_yen", "terms_unavailable_reasons"):
        item.pop(name)
    item.update(
        action=action,
        position=_position(),
        case_reassessment_reason="今回もmaterialな反証が無いことを確認した",
    )
    if action == "hold":
        item.update(
            holding_basis="investment_case_intact",
            remaining_reward=None,
            return_calculation_unavailable_reason="旧日起点valuationのため当日収益は未計算",
        )
    else:
        item.update(
            reason_code="economic_invalidation",
            remaining_reward=None,
            terms=None,
            terms_unavailable_reasons=[
                {"code": "quote_missing_or_stale", "message": "売却用の現在価格が無い"}
            ],
        )
    return payload


def test_appendix_parses_with_exact_typed_review_hash(payload):
    proposal = TradeProposal.model_validate(payload)
    assert proposal_draft_sha256(proposal) == proposal.review.draft_sha256
    assert proposal.model_dump(mode="json") == payload
    assert proposal.inputs.available_cash_yen == Decimal("300000")
    assert isinstance(proposal.items, tuple)
    assert proposal.items[0].terms.notional_yen == Decimal("300000")
    assert order_readiness(proposal.items[0]) == "ready"
    validate_proposal_policy(proposal)


def test_draft_has_the_same_hash_as_the_historical_publication(payload):
    published = TradeProposal.model_validate(payload)
    draft = load_proposal_draft(payload)
    assert "published_at" not in draft.payload()
    assert proposal_draft_sha256(draft) == published.review.draft_sha256
    assert TradeProposal.model_validate(payload).payload() == payload
    with pytest.raises(ValidationError, match="published_at"):
        TradeProposal.model_validate(draft.payload())


def test_scope_hash_binds_the_fixed_research_selection_fixture():
    path = Path(__file__).parents[3] / "fixtures/domain-assessment-proposal-synthetic.json"
    fixture = json.loads(path.read_text())
    selected = ResearchSelection.model_validate(fixture["research_selection"])
    proposal = TradeProposal.model_validate(fixture["investment_proposal"])
    assert proposal.research_scope.selection_sha256 == selection_sha256(selected)
    assert proposal.research_scope.targets == selection_targets(selected)
    assert proposal.request_scope == selected.request.scope
    assert proposal.as_of == selected.research_as_of
    assert selected.inputs.analysis.run_revision_id is None
    assert proposal.liquidity_context[0].run_revision_id is None
    assert proposal.liquidity_context[0].analysis_as_of is None
    assert proposal.liquidity_context[0].unavailable_reason == selected.inputs.input_gaps[0].reason


@pytest.mark.parametrize("adv", ["0", "-1"])
def test_liquidity_keeps_actual_nonpositive_value_for_policy_to_diagnose(payload, adv):
    payload["liquidity_context"][0]["adv_yen"] = adv
    payload["liquidity_context"][0]["unavailable_reason"] = None
    payload["liquidity_context"][0]["run_revision_id"] = (
        "run-revision-00000000000040008000000000000006"
    )
    payload["liquidity_context"][0]["analysis_as_of"] = "2026-10-14"
    proposal = TradeProposal.model_validate(payload)
    assert proposal.liquidity_context[0].adv_yen == Decimal(adv)


def test_frozen_strict_extra_and_normalization(payload):
    payload["headline"] = "  合成指値  "
    payload["items"][0]["ticker"] = " 1234 "
    proposal = TradeProposal.model_validate(payload)
    assert proposal.headline == "合成指値"
    assert proposal.items[0].ticker == "1234"
    with pytest.raises(ValidationError, match="frozen"):
        proposal.headline = "上書き"
    payload["unknown_field"] = True
    with pytest.raises(ValidationError, match="extra"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-Infinity"])
def test_nonfinite_or_boolean_money_rejected(payload, value):
    payload["inputs"]["available_cash_yen"] = value
    with pytest.raises(ValidationError, match="finite"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("value", [True, 100.0, "100"])
def test_board_lot_requires_strict_positive_integer(payload, value):
    payload["inputs"]["board_lot"] = value
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)


def test_naive_time_rejected(payload):
    payload["inputs"]["checked_at"] = "2026-10-14T16:10:00"
    with pytest.raises(ValidationError, match="timezone"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("value", [True, 3.0, "3"])
def test_version_does_not_coerce_boolean_float_or_string(payload, value):
    payload["schema_version"] = value
    with pytest.raises(ValidationError, match="must be an integer"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("field", ["prepared_at", "published_at"])
def test_review_time_must_be_between_preparation_and_publication(payload, field):
    payload[field] = (
        "2026-10-14T16:16:00+09:00" if field == "prepared_at" else "2026-10-14T16:14:00+09:00"
    )
    with pytest.raises(ValidationError, match="prepared_at"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize(
    "change", ["missing_outcome", "missing_item", "duplicate_target", "purpose"]
)
def test_scope_outcomes_and_items_must_completely_match(payload, change):
    if change == "missing_outcome":
        payload["research_scope"]["outcomes"] = []
    elif change == "missing_item":
        payload["items"] = []
    elif change == "duplicate_target":
        payload["research_scope"]["targets"] *= 2
    else:
        payload["research_scope"]["outcomes"][0]["purpose"] = "holding"
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)


def test_unfinished_target_is_defer_and_keeps_its_references(payload):
    payload["research_scope"]["targets"].append({"ticker": "5678", "purpose": "new"})
    payload["research_scope"]["outcomes"].append(
        {
            "ticker": "5678",
            "purpose": "new",
            "status": "unfinished",
            "unfinished_reason": "一次資料の確認未了",
            "references": ["https://example.invalid/unfinished"],
        }
    )
    payload["items"].append(
        {
            "ticker": "5678",
            "action": "defer",
            "rationale": "未調査を経済的な見送りへ変えない",
            "review_conditions": ["一次資料の確認"],
            "blockers": [{"code": "research_unfinished", "message": "調査未了"}],
            "resume_conditions": ["一次資料確認後に独立Reviewする"],
        }
    )
    proposal = TradeProposal.model_validate(payload)
    assert proposal.items[1].assessment_ref is None
    assert order_readiness(proposal.items[1]) == "not_applicable"
    assert proposal.research_scope.outcomes[1].references == ("https://example.invalid/unfinished",)
    payload["items"][1] = deepcopy(payload["items"][0])
    payload["items"][1]["ticker"] = "5678"
    with pytest.raises(ValidationError, match="unfinished"):
        TradeProposal.model_validate(payload)


def test_no_action_requires_completed_assessment_but_no_buy_cash_gate(payload):
    item = payload["items"][0]
    for name in ("timing", "terms", "proposed_limit_yen", "terms_unavailable_reasons"):
        item.pop(name)
    item["action"] = "no_action"
    payload["inputs"]["available_cash_yen"] = "0"
    proposal = TradeProposal.model_validate(payload)
    assert order_readiness(proposal.items[0]) == "not_applicable"
    payload["items"][0]["assessment_ref"]["core_sha256"] = "1" * 64
    with pytest.raises(ValidationError, match="reference differs"):
        TradeProposal.model_validate(payload)


def test_two_buys_parse_but_current_write_policy_rejects(payload):
    payload["inputs"]["available_cash_yen"] = "600000"
    payload["research_scope"]["targets"].append({"ticker": "5678", "purpose": "new"})
    outcome = deepcopy(payload["research_scope"]["outcomes"][0])
    outcome["ticker"] = "5678"
    payload["research_scope"]["outcomes"].append(outcome)
    item = deepcopy(payload["items"][0])
    item["ticker"] = "5678"
    payload["items"].append(item)
    for name in ("quotes", "security_classification"):
        row = deepcopy(payload["inputs"][name][0])
        row["ticker"] = "5678"
        payload["inputs"][name].append(row)
    proposal = TradeProposal.model_validate(payload)
    with pytest.raises(ProposalError, match="at most 1"):
        validate_proposal_policy(proposal)
    payload["inputs"]["available_cash_yen"] = "599999"
    with pytest.raises(ValidationError, match="combined buy notional"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize(
    "code", ["security_classification_unavailable", "price_grid_rule_unavailable"]
)
def test_buy_economic_judgment_can_have_only_classification_or_grid_terms_gap(payload, code):
    payload["items"][0]["terms"] = None
    payload["items"][0]["proposed_limit_yen"] = "1000"
    payload["items"][0]["terms_unavailable_reasons"] = [{"code": code, "message": "条件未確定"}]
    proposal = TradeProposal.model_validate(payload)
    assert order_readiness(proposal.items[0]) == "needs_terms"
    payload["items"][0]["terms_unavailable_reasons"][0]["code"] = "order_session_unknown"
    with pytest.raises(ValidationError, match="only for price grid or classification"):
        TradeProposal.model_validate(payload)


def test_terms_missing_buy_requires_an_explicit_price_ceiling(payload):
    item = payload["items"][0]
    item["terms"] = None
    item["terms_unavailable_reasons"] = [
        {"code": "security_classification_unavailable", "message": "分類確認未了"}
    ]
    with pytest.raises(ValidationError, match="requires its proposed limit ceiling"):
        TradeProposal.model_validate(payload)
    item["proposed_limit_yen"] = "900"
    assert TradeProposal.model_validate(payload).items[0].proposed_limit_yen == Decimal(900)
    item["proposed_limit_yen"] = "1100"
    with pytest.raises(ValidationError, match="actual quote timing"):
        TradeProposal.model_validate(payload)


def test_optional_price_ceiling_cannot_disagree_with_ready_terms(payload):
    payload["items"][0]["proposed_limit_yen"] = "900"
    with pytest.raises(ValidationError, match="match any confirmed buy terms"):
        TradeProposal.model_validate(payload)
    payload["items"][0]["proposed_limit_yen"] = "1000"
    assert TradeProposal.model_validate(payload).items[0].proposed_limit_yen == Decimal(1000)


@pytest.mark.parametrize(
    "change", ["missing_reason", "terms_and_reason", "sell_side", "no_maximum"]
)
def test_buy_terms_availability_and_side(payload, change):
    item = payload["items"][0]
    if change == "missing_reason":
        item["terms"] = None
    elif change == "terms_and_reason":
        item["terms_unavailable_reasons"] = [
            {"code": "price_grid_rule_unavailable", "message": "不足"}
        ]
    elif change == "sell_side":
        item["terms"]["side"] = "sell"
        item["terms"]["maximum_entry_price_yen"] = None
    else:
        item["terms"]["maximum_entry_price_yen"] = None
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", 301, "notional_yen"),
        ("notional_yen", "299999", "notional_yen"),
        ("maximum_entry_price_yen", "999", "maximum entry price"),
        ("valuation_as_of", "2026-10-13", "valuation based on proposal"),
        ("conditions", [], "at least 1"),
    ],
)
def test_terms_consistency(payload, field, value, message):
    payload["items"][0]["terms"][field] = value
    with pytest.raises(ValidationError, match=message):
        TradeProposal.model_validate(payload)


def test_terms_quantity_board_lot_and_classification_required(payload):
    terms = payload["items"][0]["terms"]
    terms.update(quantity=101, notional_yen="101000")
    with pytest.raises(ValidationError, match="board-lot"):
        TradeProposal.model_validate(payload)
    terms.update(quantity=100, notional_yen="100000")
    payload["inputs"]["security_classification"] = []
    with pytest.raises(ValidationError, match="saved security classification"):
        TradeProposal.model_validate(payload)


def test_common_grid_and_current_limit_use_actual_saved_quote(payload):
    terms = payload["items"][0]["terms"]
    terms.update(limit_price_yen="999.5", notional_yen="299850")
    with pytest.raises(ValidationError, match="common stock grid"):
        TradeProposal.model_validate(payload)
    terms.update(limit_price_yen="900", notional_yen="270000")
    payload["items"][0]["timing"] = "at_current_limit"
    with pytest.raises(ValidationError, match="actual close"):
        TradeProposal.model_validate(payload)
    payload["inputs"]["quotes"][0]["price_yen"] = "900.5"
    assert TradeProposal.model_validate(payload).items[0].terms.limit_price_yen == Decimal(900)


def test_wait_limit_must_be_below_actual_quote(payload):
    payload["inputs"]["quotes"][0]["price_yen"] = "1000"
    with pytest.raises(ValidationError, match="below the actual quote"):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("limit", ["870", "875", "880"])
def test_place_limit_preserves_quote_and_explicit_ceiling_on_either_side(payload, limit):
    payload["inputs"]["quotes"][0]["price_yen"] = "875"
    item = payload["items"][0]
    item["timing"] = "place_limit"
    item["terms"].update(limit_price_yen=limit, notional_yen=str(Decimal(limit) * 300))
    proposal = TradeProposal.model_validate(payload)
    assert proposal.inputs.quotes[0].price_yen == Decimal(875)
    assert proposal.items[0].terms.limit_price_yen == Decimal(limit)
    assert proposal_draft_sha256(proposal) != proposal.review.draft_sha256


@pytest.mark.parametrize("timing", ["at_current_limit", "wait_limit"])
def test_legacy_buy_timing_still_rejects_a_limit_above_quote(payload, timing):
    payload["inputs"]["quotes"][0]["price_yen"] = "875"
    item = payload["items"][0]
    item["timing"] = timing
    item["terms"].update(limit_price_yen="880", notional_yen="264000")
    with pytest.raises(ValidationError, match=r"actual close|below the actual quote"):
        TradeProposal.model_validate(payload)


def test_place_limit_keeps_explicit_ceiling_when_classification_prevents_terms(payload):
    payload["inputs"]["quotes"][0]["price_yen"] = "875"
    item = payload["items"][0]
    item.update(
        timing="place_limit",
        terms=None,
        proposed_limit_yen="880",
        terms_unavailable_reasons=[
            {"code": "security_classification_unavailable", "message": "分類未確認"}
        ],
    )
    proposal = TradeProposal.model_validate(payload)
    assert proposal.inputs.quotes[0].price_yen == Decimal(875)
    assert proposal.items[0].proposed_limit_yen == Decimal(880)
    assert order_readiness(proposal.items[0]) == "needs_terms"


def test_hold_on_case_intact_does_not_require_current_return_or_quote(payload):
    _holding_payload(payload)
    payload["inputs"]["quotes"] = []
    proposal = TradeProposal.model_validate(payload)
    assert order_readiness(proposal.items[0]) == "not_applicable"
    assert proposal.items[0].remaining_reward is None
    payload["items"][0]["terms"] = None
    with pytest.raises(ValidationError, match="extra"):
        TradeProposal.model_validate(payload)


def test_sufficient_hold_requires_reward_and_cannot_contradict_insufficient(payload):
    _holding_payload(payload)
    item = payload["items"][0]
    item["remaining_reward"] = {"status": "insufficient", "reason": "回収不足"}
    with pytest.raises(ValidationError, match="contradicts"):
        TradeProposal.model_validate(payload)
    item["remaining_reward"] = None
    item["holding_basis"] = "sufficient_remaining_reward"
    with pytest.raises(ValidationError, match="requires sufficient"):
        TradeProposal.model_validate(payload)
    item["remaining_reward"] = {"status": "sufficient", "reason": "当日算術と事業仮説を確認"}
    item["return_calculation_unavailable_reason"] = None
    assert (
        TradeProposal.model_validate(payload).items[0].holding_basis
        == "sufficient_remaining_reward"
    )


def test_economic_sell_without_price_or_quantity_basis_keeps_sell_judgment(payload):
    _holding_payload(payload, action="sell")
    payload["inputs"]["quotes"] = []
    payload["inputs"]["positions"][0]["quantity_basis_confirmed"] = False
    payload["items"][0]["position"]["quantity_basis_confirmed"] = False
    payload["items"][0]["terms_unavailable_reasons"].append(
        {"code": "quantity_basis_unconfirmed", "message": "現在保有の株式単位未確認"}
    )
    proposal = TradeProposal.model_validate(payload)
    assert proposal.items[0].action == "sell"
    assert order_readiness(proposal.items[0]) == "needs_terms"


@pytest.mark.parametrize("action", ["hold", "sell"])
def test_holding_judgment_preserves_negative_cash_without_a_buy_gate(payload, action):
    _holding_payload(payload, action=action)
    payload["inputs"]["available_cash_yen"] = "-1"
    proposal = TradeProposal.model_validate(payload)
    assert proposal.items[0].action == action
    assert proposal.inputs.available_cash_yen == Decimal(-1)


def test_discovery_defer_preserves_negative_cash_as_the_input_fact(payload):
    payload["request_scope"] = "discover_new"
    payload["inputs"]["available_cash_yen"] = "-1"
    item = payload["items"][0]
    for name in ("timing", "terms", "proposed_limit_yen", "terms_unavailable_reasons"):
        item.pop(name)
    item.update(
        action="defer",
        blockers=[{"code": "available_cash_below_board_lot", "message": "現在資金が不足する"}],
        resume_conditions=["資金の確認後に新しい提案を作る"],
    )
    proposal = TradeProposal.model_validate(payload)
    assert proposal.items[0].action == "defer"
    assert proposal.inputs.available_cash_yen == Decimal(-1)


def test_sell_full_holding_and_no_buy_maximum(payload):
    buy_terms = deepcopy(payload["items"][0]["terms"])
    _holding_payload(payload, action="sell")
    item = payload["items"][0]
    buy_terms.update(
        side="sell",
        limit_price_yen="1100",
        quantity=100,
        notional_yen="110000",
        maximum_entry_price_yen=None,
    )
    item.update(terms=buy_terms, terms_unavailable_reasons=[])
    assert TradeProposal.model_validate(payload).items[0].terms.side == "sell"
    for price in (900, 1101, 1200):
        buy_terms.update(limit_price_yen=str(price), notional_yen=str(price * 100))
        proposal = TradeProposal.model_validate(payload)
        assert proposal.items[0].terms.limit_price_yen == price
        assert proposal.inputs.quotes[0].price_yen == 1100
    buy_terms.update(limit_price_yen="3001", notional_yen="300100")
    with pytest.raises(ValidationError, match="grid rule"):
        TradeProposal.model_validate(payload)
    buy_terms.update(limit_price_yen="1100", notional_yen="110000")
    buy_terms.update(quantity=50, notional_yen="55000")
    with pytest.raises(ValidationError, match="entire holding"):
        TradeProposal.model_validate(payload)


def test_sell_insufficient_reward_requires_that_judgment(payload):
    _holding_payload(payload, action="sell")
    payload["items"][0]["reason_code"] = "insufficient_remaining_reward"
    with pytest.raises(ValidationError, match="requires insufficient"):
        TradeProposal.model_validate(payload)


def test_scope_actions_cannot_mix_new_and_holding(payload):
    payload["request_scope"] = "review_positions"
    with pytest.raises(ValidationError, match="target purpose"):
        TradeProposal.model_validate(payload)
    payload["research_scope"]["targets"][0]["purpose"] = "holding"
    payload["research_scope"]["outcomes"][0]["purpose"] = "holding"
    with pytest.raises(ValidationError, match="holding scope"):
        TradeProposal.model_validate(payload)


def test_draft_hash_excludes_only_review_and_publication_time(payload):
    proposal = TradeProposal.model_validate(payload)
    baseline = proposal_draft_sha256(proposal)
    payload["review"]["reviewer_identity"] = "別reviewer"
    payload["published_at"] = "2026-10-14T16:25:00+09:00"
    assert proposal_draft_sha256(TradeProposal.model_validate(payload)) == baseline
    payload["inputs"]["checked_at"] = "2026-10-14T16:11:00+09:00"
    assert proposal_draft_sha256(TradeProposal.model_validate(payload)) != baseline


@pytest.mark.parametrize(
    "change", ["cash", "scope", "condition", "warning", "exposure", "liquidity"]
)
def test_review_draft_hash_covers_saved_inputs_terms_scope_and_projections(payload, change):
    baseline = proposal_draft_sha256(TradeProposal.model_validate(payload))
    if change == "cash":
        payload["inputs"]["available_cash_yen"] = "300001"
    elif change == "scope":
        payload["research_scope"]["selection_sha256"] = "1" * 64
    elif change == "condition":
        payload["items"][0]["terms"]["conditions"].append("現在previewで入力一致を確認")
    elif change == "warning":
        payload["warnings"].append("portfolio_valuation_unknown:5678")
    elif change == "exposure":
        payload["portfolio_exposure"]["ticker"]["prospective_pct"] = "99"
    else:
        payload["liquidity_context"][0]["unavailable_reason"] = "元snapshotにADV無し"
    assert proposal_draft_sha256(TradeProposal.model_validate(payload)) != baseline


def test_quote_uses_native_daily_date_without_an_observation_clock(payload):
    proposal = TradeProposal.model_validate(payload)
    quote = proposal.inputs.quotes[0]
    assert quote.price_as_of.isoformat() == "2026-10-14"
    assert "observed_at" not in quote.model_dump()
    payload["inputs"]["quotes"][0]["observed_at"] = "2026-10-14T15:30:00+09:00"
    with pytest.raises(ValidationError, match="Extra inputs"):
        TradeProposal.model_validate(payload)


def test_order_terms_require_saved_common_stock_classification(payload):
    payload["inputs"]["security_classification"][0]["is_common_stock"] = False
    with pytest.raises(ValidationError, match="common stock grid rule"):
        TradeProposal.model_validate(payload)


def test_proposal_terms_do_not_require_or_accept_a_duplicate_expiry(payload):
    proposal = TradeProposal.model_validate(payload)
    assert proposal.items[0].terms.target_session.isoformat() == "2026-10-15"
    assert "expires_at" not in proposal.items[0].terms.model_dump()
    payload["items"][0]["terms"]["expires_at"] = "2026-10-15T15:30:00+09:00"
    with pytest.raises(ValidationError, match="Extra inputs"):
        TradeProposal.model_validate(payload)


def test_proposal_rejects_prior_schema_without_relabeling_review(payload):
    payload["schema_version"] = 2
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)
