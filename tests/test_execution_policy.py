from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import PortfolioLedgerDocument, reconcile_portfolio
from baibai_loop.thesis.decision_cli import main as decision_cli_main
from baibai_loop.thesis.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketResult,
    decision_packet_core_hash,
    evaluate_decision_packet,
    load_decision_packet,
    load_independent_review,
)
from baibai_loop.thesis.execution_policy import (
    ExecutionOutcomeInput,
    ExecutionPolicyError,
    ExecutionPolicyInput,
    ExecutionProposal,
    evaluate_execution_outcome,
    evaluate_execution_policy,
    max_acceptable_price,
    portfolio_input_from_snapshot,
)

ROOT = Path(__file__).parents[1]
PACKET = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"
POLICY = ROOT / "tests/fixtures/execution-policy/current-ladder.yaml"
OUTCOME = ROOT / "tests/fixtures/execution-policy/not-filled-outcome.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
EVALUATED_AT = datetime.fromisoformat("2026-07-11T10:01:00+09:00")


def _raw(path: Path) -> dict[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _policy(raw: dict[str, object] | None = None) -> ExecutionPolicyInput:
    return ExecutionPolicyInput.model_validate(raw or _raw(POLICY))


def _packet() -> tuple[DecisionPacketDocument, DecisionPacketResult]:
    document = load_decision_packet(PACKET)
    result = evaluate_decision_packet(document, review=load_independent_review(REVIEW))
    assert result.decision_readiness == "ready"
    return document, result


def _proposal(raw: dict[str, object] | None = None) -> ExecutionProposal:
    document, result = _packet()
    return evaluate_execution_policy(document, result, _policy(raw))


def test_max_price_is_recalculated_from_5y_base_and_required_return() -> None:
    document, _ = _packet()

    max_price = max_acceptable_price(document, tick_size_yen=Decimal("1"))

    assert max_price == 1109
    assert max_price != document.estimates.entry_price_basis_yen


def test_portfolio_input_is_adapted_from_the_canonical_ledger_snapshot() -> None:
    snapshot = reconcile_portfolio(PortfolioLedgerDocument.model_validate(_raw(LEDGER)))

    policy_portfolio = portfolio_input_from_snapshot(
        snapshot,
        ticker="2331",
        sector="サービス業",
        common_factors=("labor-automation",),
    )

    assert policy_portfolio.available_cash_yen == snapshot.available_cash_yen
    assert policy_portfolio.as_of == snapshot.as_of
    assert policy_portfolio.reserved_cash_yen == snapshot.reserved_cash_yen
    assert policy_portfolio.total_capital_yen == snapshot.total_capital_yen
    assert policy_portfolio.dry_powder_floor_yen == snapshot.total_capital_yen // 5


def test_balanced_policy_uses_buy_now_when_ask_is_within_ceiling_without_warning() -> None:
    proposal = _proposal()

    assert proposal.recommended_tactic == "buy_now"
    assert [(order.tactic, order.quantity, order.limit_price_yen) for order in proposal.orders] == [
        ("buy_now", 200, Decimal("1105"))
    ]
    assert all(
        order.limit_price_yen <= proposal.max_acceptable_price_yen for order in proposal.orders
    )


def test_ask_above_ceiling_cannot_be_bought_now_but_shallow_limit_remains_available() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["bid_yen"] = 1108
    quote["ask_yen"] = 1110

    proposal = _proposal(raw)

    buy_now = proposal.options[0]
    assert buy_now.eligible is False
    assert "ask_above_max_acceptable_price" in buy_now.reasons
    assert proposal.recommended_tactic == "shallow_limit"


def test_all_visible_quotes_above_ceiling_defers_without_chasing_price() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["bid_yen"] = 1110
    quote["ask_yen"] = 1111

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "defer"
    assert proposal.orders == ()
    assert "all_visible_quotes_above_max_acceptable_price" in proposal.warnings


def test_wide_spread_moves_balanced_policy_to_limit_and_reports_observation() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["bid_yen"] = 1000
    quote["ask_yen"] = 1105

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "shallow_limit"
    assert "spread_exceeds_explicit_warning" in proposal.warnings


def test_thin_visible_ask_is_warning_not_price_escalation() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["ask_size"] = 100

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "shallow_limit"
    assert "visible_ask_size_below_requested_quantity" in proposal.warnings
    assert all(
        order.limit_price_yen <= proposal.max_acceptable_price_yen for order in proposal.orders
    )


def test_missing_book_uses_max_price_fallback_for_a_limit_not_a_claimed_quote() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["bid_yen"] = None
    quote["bid_size"] = None
    quote["ask_yen"] = None
    quote["ask_size"] = None

    proposal = _proposal(raw)

    shallow = proposal.options[1]
    assert shallow.eligible is True
    assert shallow.price_yen == proposal.max_acceptable_price_yen
    assert "max_acceptable_price_fallback_without_bid" in shallow.reasons


def test_stale_quote_is_deferred_even_when_its_price_is_attractive() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["freshness_status"] = "stale"

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "defer"
    assert proposal.orders == ()
    assert "quote_is_not_currently_actionable" in proposal.warnings


def test_old_quote_cannot_claim_current_freshness() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["observed_at"] = "2020-01-01T10:00:00+09:00"

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "defer"


def test_quote_older_than_the_fixed_current_snapshot_window_is_deferred() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["observed_at"] = "2026-07-11T09:55:00+09:00"

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "defer"
    assert "quote_is_not_currently_actionable" in proposal.warnings


def test_policy_is_reproducible_from_its_explicit_evaluation_snapshot() -> None:
    assert _proposal() == _proposal()


def test_policy_requires_a_current_canonical_ledger_snapshot() -> None:
    raw = _raw(POLICY)
    portfolio = raw["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["as_of"] = "2026-07-11T09:50:00+09:00"

    with pytest.raises(ExecutionPolicyError, match="ledger snapshot must be current"):
        _proposal(raw)


def test_quote_price_must_align_to_its_declared_tick_size() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["ask_yen"] = 1105.5

    with pytest.raises(ValueError, match="must align to tick_size_yen"):
        _policy(raw)


def test_cash_boundary_defers_instead_of_consuming_dry_powder() -> None:
    raw = _raw(POLICY)
    portfolio = raw["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["available_cash_yen"] = 300000

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "defer"
    assert proposal.cash_after_execution_yen == 300000


def test_policy_warns_when_the_order_would_exceed_ticker_concentration() -> None:
    raw = _raw(POLICY)
    portfolio = raw["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["ticker_exposure_yen"] = 620000

    proposal = _proposal(raw)

    assert "prospective_ticker_concentration_exceeds_warning" in proposal.warnings
    assert proposal.prospective_concentration_warnings == (
        "prospective_ticker_concentration_exceeds_warning",
    )


def test_one_lot_price_priority_never_disguises_a_ladder() -> None:
    raw = _raw(POLICY)
    raw["quantity"] = 100
    raw["fill_priority"] = "price"

    proposal = _proposal(raw)

    assert proposal.recommended_tactic == "deep_limit"
    assert [(order.tactic, order.quantity) for order in proposal.orders] == [("deep_limit", 100)]


def test_two_lot_limit_policy_creates_one_shallow_and_remaining_deep_lot() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["ask_size"] = 100

    proposal = _proposal(raw)

    assert [(order.tactic, order.quantity) for order in proposal.orders] == [
        ("shallow_limit", 100),
        ("deep_limit", 100),
    ]
    assert sum(order.quantity for order in proposal.orders) == 200


def test_equal_deep_tick_uses_a_single_shallow_order() -> None:
    raw = _raw(POLICY)
    quote = raw["quote"]
    assert isinstance(quote, dict)
    quote["tick_size_yen"] = 100
    quote["last_yen"] = 1100
    quote["bid_yen"] = 1100
    quote["ask_yen"] = 1100
    quote["ask_size"] = 100
    quote["bid_depth"] = []
    quote["ask_depth"] = []
    raw["deep_discount_bps"] = 0
    packet_raw = _raw(PACKET)
    estimates = packet_raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["deep_discount_bps"] = 0
    document = DecisionPacketDocument.model_validate(packet_raw)
    result = replace(_packet()[1], packet_sha256=decision_packet_core_hash(document))

    proposal = evaluate_execution_policy(document, result, _policy(raw))

    assert proposal.recommended_tactic == "shallow_limit"
    assert [(order.tactic, order.quantity) for order in proposal.orders] == [("shallow_limit", 200)]


def test_policy_rejects_a_deep_discount_that_is_not_bound_to_the_packet() -> None:
    raw = _raw(POLICY)
    raw["deep_discount_bps"] = 200

    with pytest.raises(ExecutionPolicyError, match="deep discount must match"):
        _proposal(raw)


def test_policy_rejects_sector_and_common_factor_not_bound_to_the_packet() -> None:
    raw = _raw(POLICY)
    raw["sector"] = "情報・通信業"

    with pytest.raises(ExecutionPolicyError, match="sector must match"):
        _proposal(raw)

    raw = _raw(POLICY)
    raw["common_factors"] = ["ai-demand"]

    with pytest.raises(ExecutionPolicyError, match="common factors must match"):
        _proposal(raw)


def test_policy_rejects_packet_that_is_not_decision_ready() -> None:
    document, result = _packet()
    raw = _raw(POLICY)
    not_ready = result.__class__(
        packet_status="incomplete",
        decision_readiness="not_ready",
        packet_sha256=result.packet_sha256,
        errors=("fixture",),
        warnings=(),
        scenarios=result.scenarios,
    )

    with pytest.raises(ExecutionPolicyError, match="decision-ready"):
        evaluate_execution_policy(document, not_ready, _policy(raw))


def test_policy_rejects_a_ready_packet_that_does_not_recommend_buy() -> None:
    raw = _raw(PACKET)
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["recommendation"] = "defer"
    document = DecisionPacketDocument.model_validate(raw)
    result = evaluate_decision_packet(document)
    assert result.decision_readiness == "ready"

    with pytest.raises(ExecutionPolicyError, match="buy recommendation"):
        evaluate_execution_policy(document, result, _policy())


def test_policy_rejects_a_result_that_is_bound_to_another_packet() -> None:
    raw = _raw(PACKET)
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["required_5y_base_cagr_pct"] = 9.0
    changed_document = DecisionPacketDocument.model_validate(raw)
    original_result = _packet()[1]

    with pytest.raises(ExecutionPolicyError, match="must match the decision packet"):
        evaluate_execution_policy(changed_document, original_result, _policy())


def test_not_filled_outcome_records_touch_without_turning_it_into_a_fill() -> None:
    outcome = evaluate_execution_outcome(ExecutionOutcomeInput.model_validate(_raw(OUTCOME)))

    assert outcome.unfilled_quantity == 100
    assert outcome.touch_within_window == "true"
    assert outcome.first_touch_date is not None
    assert outcome.post_expiry_price_yen == 1155
    assert outcome.missed_upside_yen == 5000


def test_not_filled_outcome_is_unresolved_when_price_bases_do_not_match() -> None:
    raw = _raw(OUTCOME)
    bars = raw["bars"]
    assert isinstance(bars, list)
    for bar in bars:
        assert isinstance(bar, dict)
        bar["basis_group_id"] = "adjusted-other-group"

    outcome = evaluate_execution_outcome(ExecutionOutcomeInput.model_validate(raw))

    assert outcome.touch_within_window == "unresolved"
    assert outcome.missed_upside_yen is None


def test_not_filled_outcome_rejects_pre_submission_daily_low_as_a_touch() -> None:
    raw = _raw(OUTCOME)
    bars = raw["bars"]
    assert isinstance(bars, list)
    bars[:] = [bar for bar in bars if isinstance(bar, dict) and bar["trade_date"] == "2026-07-03"]

    outcome = evaluate_execution_outcome(ExecutionOutcomeInput.model_validate(raw))

    assert outcome.touch_within_window == "unresolved"


@pytest.mark.parametrize("observed_at", ["2026-07-03T10:01:00+09:00", "2026-07-03T09:50:00+09:00"])
def test_not_filled_outcome_rejects_future_or_stale_decision_quote(observed_at: str) -> None:
    raw = _raw(OUTCOME)
    decision_quote = raw["decision_quote"]
    assert isinstance(decision_quote, dict)
    decision_quote["observed_at"] = observed_at

    with pytest.raises(ValueError, match="decision quote"):
        ExecutionOutcomeInput.model_validate(raw)


@pytest.mark.parametrize(
    ("terminal_reason", "filled_quantity", "message"),
    [
        ("expired", 100, "requires unfilled quantity"),
        ("broker_rejected", 1, "cannot contain a fill"),
    ],
)
def test_not_filled_outcome_matches_terminal_fill_invariants(
    terminal_reason: str, filled_quantity: int, message: str
) -> None:
    raw = _raw(OUTCOME)
    raw["terminal_reason"] = terminal_reason
    raw["filled_quantity"] = filled_quantity
    if terminal_reason == "broker_rejected":
        raw["terminal_at"] = "2026-07-07T15:29:00+09:00"

    with pytest.raises(ValueError, match=message):
        ExecutionOutcomeInput.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_basis", "last_close_adjusted"),
        ("corporate_action_checked", False),
        ("ticker", "9999"),
    ],
)
def test_not_filled_outcome_requires_same_ticker_basis_and_corporate_action_check(
    field: str, value: object
) -> None:
    raw = _raw(OUTCOME)
    bars = raw["bars"]
    assert isinstance(bars, list)
    target = bars[1]
    assert isinstance(target, dict)
    target[field] = value

    outcome = evaluate_execution_outcome(ExecutionOutcomeInput.model_validate(raw))

    assert outcome.touch_within_window == "unresolved"
    assert outcome.missed_upside_yen is None


def test_decision_cli_includes_execution_proposal_when_input_is_supplied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = decision_cli_main(
        [str(PACKET), "--execution-input", str(POLICY), "--ledger", str(LEDGER)],
        now=EVALUATED_AT,
    )

    payload = yaml.safe_load(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["execution_proposal"]["recommended_tactic"] == "buy_now"
    assert payload["execution_proposal"]["evaluated_at"] == "2026-07-11T10:01:00+09:00"


def test_decision_cli_rejects_a_historical_execution_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = decision_cli_main(
        [str(PACKET), "--execution-input", str(POLICY), "--ledger", str(LEDGER)],
        now=datetime.fromisoformat("2026-07-11T10:07:00+09:00"),
    )

    assert exit_code == 2
    assert "evaluation must be current" in capsys.readouterr().err


def test_decision_cli_rejects_an_execution_input_that_has_already_expired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _raw(POLICY)
    raw["expires_at"] = "2026-07-11T10:02:00+09:00"
    input_path = tmp_path / "expired-execution-input.yaml"
    input_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    exit_code = decision_cli_main(
        [str(PACKET), "--execution-input", str(input_path), "--ledger", str(LEDGER)],
        now=datetime.fromisoformat("2026-07-11T10:03:00+09:00"),
    )

    assert exit_code == 2
    assert "input has expired" in capsys.readouterr().err


def test_decision_cli_rejects_execution_input_without_a_canonical_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = decision_cli_main([str(PACKET), "--execution-input", str(POLICY)])

    assert exit_code == 2
    assert "requires --ledger" in capsys.readouterr().err


def test_decision_cli_rejects_hand_edited_cash_that_differs_from_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _raw(POLICY)
    portfolio = raw["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["available_cash_yen"] = 99999999
    input_path = tmp_path / "execution-input.yaml"
    input_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    exit_code = decision_cli_main(
        [str(PACKET), "--execution-input", str(input_path), "--ledger", str(LEDGER)],
        now=EVALUATED_AT,
    )

    assert exit_code == 2
    assert "must match the canonical ledger snapshot" in capsys.readouterr().err
