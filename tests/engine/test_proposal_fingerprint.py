from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.research.proposal import ProposalInputs
from baibai_engine.research.proposal_fingerprint import proposal_inputs_fingerprint


@pytest.fixture
def facts():
    fixture = Path(__file__).parents[1] / "fixtures/domain-assessment-proposal-synthetic.json"
    facts = json.loads(fixture.read_text())["investment_proposal"]["inputs"]
    facts["positions"] = [
        {
            "ticker": "5678",
            "sector": "情報・通信業",
            "common_factors": ["domestic-demand", "recurring-revenue"],
            "quantity": 100,
            "cost_yen": "90000",
            "quantity_basis_confirmed": True,
            "original_assessment_ref": None,
            "original_investment_reason": "継続利益を見込む",
            "original_reason_unavailable_reason": None,
        }
    ]
    facts["active_reservations"] = [
        {
            "reservation_id": "synthetic-order-1",
            "ticker": "9012",
            "reserved_yen": "50000",
            "decision_reference": "synthetic-decision-1",
            "sector": "情報・通信業",
            "common_factors": ["domestic-demand", "recurring-revenue"],
        }
    ]
    facts["prior_buy_execution_references"] = ["synthetic-fill-1", "synthetic-fill-2"]
    return facts


def _fingerprint(facts):
    return proposal_inputs_fingerprint(ProposalInputs.model_validate(facts))


def test_checked_at_is_the_only_excluded_fact(facts):
    baseline = _fingerprint(facts)
    facts["checked_at"] = "2026-10-14T16:20:00+09:00"
    assert _fingerprint(facts) == baseline
    facts["ledger_as_of"] = "2026-10-14T15:01:00+09:00"
    assert _fingerprint(facts) != baseline


def test_decimal_equivalence_and_utc_same_instant_do_not_conflict(facts):
    baseline = _fingerprint(facts)
    facts["ledger_as_of"] = "2026-10-14T06:00:00Z"
    facts["available_cash_yen"] = "3.00000E+5"
    facts["total_capital_yen"] = "300000.00"
    facts["positions"][0]["cost_yen"] = "90000.000"
    facts["active_reservations"][0]["reserved_yen"] = "5E+4"
    facts["quotes"][0]["price_yen"] = "1100.0000"
    assert _fingerprint(facts) == baseline


def test_array_order_and_common_factor_order_are_stable(facts):
    for name, identity in (
        ("positions", "ticker"),
        ("quotes", "ticker"),
        ("active_reservations", "reservation_id"),
        ("security_classification", "ticker"),
    ):
        extra = deepcopy(facts[name][0])
        extra[identity] = "synthetic-order-2" if identity == "reservation_id" else "2345"
        facts[name].append(extra)
    baseline = _fingerprint(facts)
    for name in (
        "positions",
        "quotes",
        "active_reservations",
        "security_classification",
        "prior_buy_execution_references",
    ):
        facts[name].reverse()
    for name in ("positions", "active_reservations"):
        for row in facts[name]:
            row["common_factors"].reverse()
    assert _fingerprint(facts) == baseline


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("ledger_head",), 2),
        (("ledger_as_of",), "2026-10-14T15:01:00+09:00"),
        (("available_cash_yen",), "300001"),
        (("total_capital_yen",), None),
        (("policy_id",), "changed-policy"),
        (("budget_min_yen",), 100001),
        (("budget_max_yen",), 300001),
        (("board_lot",), 10),
        (("prior_buy_execution_references", 0), "changed-fill"),
        (("positions", 0, "quantity"), 200),
        (("positions", 0, "cost_yen"), "90001"),
        (("positions", 0, "quantity_basis_confirmed"), False),
        (("positions", 0, "original_investment_reason"), "当初理由の訂正"),
        (("positions", 0, "sector"), "サービス業"),
        (("positions", 0, "common_factors", 0), "export-demand"),
        (("active_reservations", 0, "reservation_id"), "changed-order"),
        (("active_reservations", 0, "ticker"), "9013"),
        (("active_reservations", 0, "reserved_yen"), "50001"),
        (("active_reservations", 0, "decision_reference"), "changed-decision"),
        (("quotes", 0, "price_yen"), "1101"),
        (("quotes", 0, "price_as_of"), "2026-10-13"),
        (("quotes", 0, "source_ref"), "fixture:other-received-version"),
        (("security_classification", 0, "market_segment"), "Standard"),
        (("security_classification", 0, "is_common_stock"), False),
        (("security_classification", 0, "snapshot_date"), "2026-10-13"),
    ],
)
def test_every_actual_input_change_is_in_the_fingerprint(facts, path, value):
    baseline = _fingerprint(facts)
    target = facts
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert _fingerprint(facts) != baseline


def test_decimal_canonicalization_does_not_round_long_values(facts):
    facts["positions"][0]["cost_yen"] = "12345678901234567890123456789.0000000001"
    baseline = _fingerprint(facts)
    facts["positions"][0]["cost_yen"] += "00"
    assert _fingerprint(facts) == baseline
    facts["positions"][0]["cost_yen"] = "12345678901234567890123456789.0000000002"
    assert _fingerprint(facts) != baseline


def test_input_missing_nullable_field_is_not_filled(facts):
    facts.pop("total_capital_yen")
    with pytest.raises(ValidationError, match="required"):
        ProposalInputs.model_validate(facts)


def test_future_ledger_or_master_and_duplicate_quote_are_rejected(facts):
    facts["ledger_as_of"] = "2026-10-14T16:11:00+09:00"
    with pytest.raises(ValidationError, match="ledger_as_of"):
        ProposalInputs.model_validate(facts)
    facts["ledger_as_of"] = "2026-10-14T15:00:00+09:00"
    facts["security_classification"][0]["snapshot_date"] = "2026-10-15"
    with pytest.raises(ValidationError, match="classification snapshot"):
        ProposalInputs.model_validate(facts)
    facts["security_classification"][0]["snapshot_date"] = "2026-10-14"
    facts["quotes"] *= 2
    with pytest.raises(ValidationError, match="unique identities"):
        ProposalInputs.model_validate(facts)
