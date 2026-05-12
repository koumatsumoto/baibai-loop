from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.validate.trade import discover_trade_files, validate_trade_file


def _snapshot(ref_path: str) -> dict[str, object]:
    return {"ref_path": ref_path}


def _calendar_snapshots() -> dict[str, object]:
    return {
        "business_days": _snapshot("records/_calendars/business-days/2026-05.yaml"),
        "events": _snapshot("records/_calendars/events/2026-05.yaml"),
        "corporate_actions": _snapshot("records/_calendars/corporate-actions/2026-05.yaml"),
    }


def _trade_front(**overrides: object) -> dict[str, object]:
    front: dict[str, object] = {
        "trade_id": "trade-20260505-9682",
        "ticker": "9682",
        "research_ref": "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md",
        "policy_ref": _snapshot(
            "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
        ),
        "portfolio_exposure_ref": _snapshot(
            "records/_portfolio-exposure/2026/05/2026-05-05T200000+0900.yaml"
        ),
        "policy_applicability": "active",
        "calendar_refs": _calendar_snapshots(),
        "position_state": "open",
        "current_quantity": 200,
        "review_state": "not_due",
        "trade_execution_state": "filled",
        "order_intent": {
            "order_intent_id": "intent-20260505-9682-entry",
            "decision_event_id": "decision-20260505-9682-trade",
            "side": "buy",
            "quantity": 200,
            "order_price_guard_yen": 1050,
            "uses_margin": False,
            "not_submitted_reason": None,
        },
        "position_sizing_overlay": {
            "guarded_max_notional_yen": 210000,
            "estimated_real_order_notional_yen": 210000,
        },
        "entry_legs": [
            {
                "entry_leg_id": "entry-20260505-9682-1",
                "research_ref": "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md",
                "order_id": "order-20260505-9682-entry",
                "quantity": 200,
                "average_price_yen": 1014,
                "conviction_tier": "medium",
            }
        ],
        "orders": [
            {
                "order_id": "order-20260505-9682-entry",
                "origin_order_intent_id": "intent-20260505-9682-entry",
                "side": "buy",
                "state": "filled",
                "submitted_quantity": 200,
                "filled_quantity": 200,
                "events": [
                    {"event_type": "submit", "at": "2026-05-05T09:00:00+09:00"},
                    {"event_type": "fill", "at": "2026-05-05T09:01:00+09:00"},
                ],
            }
        ],
        "executions": [
            {
                "execution_id": "exec-20260505-9682-entry-1",
                "order_id": "order-20260505-9682-entry",
                "side": "buy",
                "quantity": 200,
                "price_yen": 1014,
                "at": "2026-05-05T09:01:00+09:00",
            }
        ],
        "kill_switch_check": {
            "earnings_straddle": False,
            "boj_eve": False,
            "fomc_eve": False,
            "no_margin_trading": False,
        },
    }
    front.update(overrides)
    return front


def _write_trade(
    tmp_path: Path, front: dict[str, object] | None = None, name: str = "2026-05-05-9682.md"
) -> Path:
    root = _test_repo_root(tmp_path)
    _write_test_repo_sources(root)
    path = tmp_path / name if _is_trade_dir(tmp_path) else root / "records/06-trades/2026/05" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = front if front is not None else _trade_front()
    path.write_text(
        "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) + "---\n\n# Trade\n",
        encoding="utf-8",
    )
    return path


def _test_repo_root(path: Path) -> Path:
    parts = path.parts
    if "records" not in parts:
        return path
    return Path(*parts[: parts.index("records")])


def _is_trade_dir(path: Path) -> bool:
    parts = path.parts
    return "records" in parts and "06-trades" in parts


def _write_test_repo_sources(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    policy_path = root / "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    if not policy_path.exists():
        policy_path.write_text(
            "---\n"
            "order_constraints:\n"
            "  board_lot: 100\n"
            "unique_constraints:\n"
            "- id: no-margin-trading\n"
            "  validator_callable_id: no_margin_trading\n"
            "---\n\n# Policy\n",
            encoding="utf-8",
        )
    for rel_path, payload in {
        "records/_calendars/business-days/2026-05.yaml": {"business_days": ["2026-05-05"]},
        "records/_calendars/events/2026-05.yaml": {"events": []},
        "records/_calendars/corporate-actions/2026-05.yaml": {"events": []},
        "records/_portfolio-exposure/2026/05/2026-05-05T200000+0900.yaml": {
            "as_of": "2026-05-05T20:00:00+09:00",
            "remaining_tactical_budget_yen": 1000000,
            "source_trade_refs": [],
            "source_decision_register_refs": [],
        },
    }.items():
        source_path = root / rel_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            source_path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
    research_path = root / "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md"
    research_path.parent.mkdir(parents=True, exist_ok=True)
    if not research_path.exists():
        research_path.write_text(
            "---\n"
            "research_decision:\n"
            "  outcome: approved\n"
            "  posture: act_now\n"
            "thesis_payoff:\n"
            "  max_entry_price_yen: 1050\n"
            "position_sizing_overlay:\n"
            "  real_order_intent_yen: 210000\n"
            "---\n\n# Research\n",
            encoding="utf-8",
        )
    register_path = root / "records/_ledger/research-decisions/2026-05.jsonl"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    if not register_path.exists():
        register_path.write_text(
            '{"decision_event_id":"decision-20260505-9682-trade",'
            '"order_intent":{"order_intent_id":"intent-20260505-9682-entry"}}\n',
            encoding="utf-8",
        )


def test_trade_with_filled_order_lifecycle_passes(tmp_path: Path) -> None:
    path = _write_trade(tmp_path)
    assert [finding for finding in validate_trade_file(path) if finding.severity == "error"] == []


def test_missing_required_field_is_flagged(tmp_path: Path) -> None:
    front = _trade_front()
    del front["trade_id"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.required" in codes


def test_removed_legacy_field_is_flagged(tmp_path: Path) -> None:
    front = _trade_front(status="open")
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.removed-field" in codes


def test_missing_policy_applicability_is_flagged(tmp_path: Path) -> None:
    front = _trade_front()
    del front["policy_applicability"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.policy-applicability" in codes


def test_missing_calendar_refs_is_flagged(tmp_path: Path) -> None:
    front = _trade_front()
    del front["calendar_refs"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.calendar-refs" in codes


def test_invalid_policy_ref_is_flagged_by_trade_target(tmp_path: Path) -> None:
    front = _trade_front(policy_ref={"ref_path": "/tmp/policy.md"})
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.reference-ref" in codes


def test_wrong_calendar_ref_prefix_is_flagged_by_trade_target(tmp_path: Path) -> None:
    calendars = _calendar_snapshots()
    calendars["events"] = _snapshot("records/_calendars/business-days/2026-05.yaml")
    front = _trade_front(calendar_refs=calendars)
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.calendar-ref" in codes


def test_nested_removed_hash_field_is_flagged(tmp_path: Path) -> None:
    front = _trade_front()
    order_intent = front["order_intent"]
    assert isinstance(order_intent, dict)
    order_intent["content_" + "sha256"] = "sha256:bad"
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.removed-hash-field" in codes


def test_nested_removed_reference_field_is_flagged(tmp_path: Path) -> None:
    front = _trade_front()
    policy_ref = front["policy_ref"]
    assert isinstance(policy_ref, dict)
    policy_ref["snapshot_path"] = "records/01-policy/2026/05/policy.md"
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.removed-reference-field" in codes


def test_order_intent_must_join_to_order(tmp_path: Path) -> None:
    front = _trade_front()
    orders = front["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    order["origin_order_intent_id"] = "intent-other"
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.order-intent-join" in codes


def test_submitted_trade_requires_order_intent_fields(tmp_path: Path) -> None:
    front = _trade_front()
    front["order_intent"] = {}
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.order-intent-field" in codes


def test_submitted_trade_requires_orders(tmp_path: Path) -> None:
    front = _trade_front()
    front["orders"] = []
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.orders-required" in codes


def test_submitted_trade_requires_position_sizing_overlay(tmp_path: Path) -> None:
    front = _trade_front()
    del front["position_sizing_overlay"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.required" in codes
    assert "trade.position-sizing-required" in codes


def test_submitted_trade_schema_requires_sizing_fields(tmp_path: Path) -> None:
    front = _trade_front()
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    del sizing["guarded_max_notional_yen"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.required" in codes
    assert "trade.position-sizing-field" in codes


def test_order_state_is_validated(tmp_path: Path) -> None:
    front = _trade_front()
    orders = front["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    order["state"] = "open"
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.order-state" in codes


def test_filled_quantity_cannot_exceed_submitted_quantity(tmp_path: Path) -> None:
    front = _trade_front()
    orders = front["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    order["filled_quantity"] = 300
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.filled-quantity" in codes


def test_position_state_none_cannot_have_executions(tmp_path: Path) -> None:
    front = _trade_front(position_state="none")
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.position-execution-state" in codes


def test_current_quantity_is_recomputed_from_executions(tmp_path: Path) -> None:
    front = _trade_front()
    front["current_quantity"] = 100
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.current-quantity" in codes


def test_guarded_notional_must_match_quantity_times_guard(tmp_path: Path) -> None:
    front = _trade_front()
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    sizing["guarded_max_notional_yen"] = 202800
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.guarded-notional" in codes


def test_order_quantity_is_recomputed_from_research_intent(tmp_path: Path) -> None:
    front = _trade_front()
    intent = front["order_intent"]
    sizing = front["position_sizing_overlay"]
    assert isinstance(intent, dict)
    assert isinstance(sizing, dict)
    intent["quantity"] = 100
    sizing["guarded_max_notional_yen"] = 105000
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.intent-derived" in codes


def test_submitted_trade_requires_positive_quantity(tmp_path: Path) -> None:
    front = _trade_front(current_quantity=0, executions=[], entry_legs=[])
    intent = front["order_intent"]
    sizing = front["position_sizing_overlay"]
    orders = front["orders"]
    assert isinstance(intent, dict)
    assert isinstance(sizing, dict)
    assert isinstance(orders, list)
    intent["quantity"] = 0
    sizing["guarded_max_notional_yen"] = 0
    order = orders[0]
    assert isinstance(order, dict)
    order["submitted_quantity"] = 0
    order["filled_quantity"] = 0
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.order-intent-quantity" in codes


def test_submitted_trade_requires_approved_research_ref(tmp_path: Path) -> None:
    front = _trade_front(current_quantity=0, executions=[], entry_legs=[])
    intent = front["order_intent"]
    sizing = front["position_sizing_overlay"]
    orders = front["orders"]
    assert isinstance(intent, dict)
    assert isinstance(sizing, dict)
    assert isinstance(orders, list)
    intent["quantity"] = 0
    sizing["guarded_max_notional_yen"] = 0
    order = orders[0]
    assert isinstance(order, dict)
    order["submitted_quantity"] = 0
    order["filled_quantity"] = 0
    root = _test_repo_root(tmp_path)
    research_path = root / "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md"
    research_path.parent.mkdir(parents=True, exist_ok=True)
    research_path.write_text(
        "---\n"
        "research_decision:\n"
        "  outcome: deferred\n"
        "  posture: wait_for_event\n"
        "thesis_payoff:\n"
        "  max_entry_price_yen: 1050\n"
        "position_sizing_overlay:\n"
        "  real_order_intent_yen: 0\n"
        "---\n\n# Research\n",
        encoding="utf-8",
    )
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.research-approval" in codes


def test_order_guard_is_recomputed_from_research_payoff(tmp_path: Path) -> None:
    front = _trade_front()
    intent = front["order_intent"]
    sizing = front["position_sizing_overlay"]
    assert isinstance(intent, dict)
    assert isinstance(sizing, dict)
    intent["order_price_guard_yen"] = 1000
    intent["quantity"] = 200
    sizing["guarded_max_notional_yen"] = 200000
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.intent-derived" in codes


def test_submitted_trade_requires_valid_research_ref_for_order_intent(tmp_path: Path) -> None:
    front = _trade_front()
    front["research_ref"] = "records/05-research/missing.md"
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.intent-source" in codes


def test_submitted_trade_requires_entry_legs(tmp_path: Path) -> None:
    front = _trade_front()
    del front["entry_legs"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.entry-legs-required" in codes


def test_trade_intent_must_join_decision_register(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    register = tmp_path / "records/_ledger/research-decisions/2026-05.jsonl"
    register.parent.mkdir(parents=True)
    register.write_text(
        '{"decision_event_id":"decision-20260505-9682-trade",'
        '"order_intent":{"order_intent_id":"intent-other"}}\n',
        encoding="utf-8",
    )
    path = _write_trade(tmp_path / "records/06-trades/2026/05", _trade_front())
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.decision-register-intent-join" in codes


def test_kill_switch_check_is_recomputed_from_events_calendar(tmp_path: Path) -> None:
    policy_ref = "records/01-policy/2026/05/policy.yaml"
    policy = tmp_path / policy_ref
    policy.parent.mkdir(parents=True)
    policy.write_text(
        yaml.safe_dump(
            {
                "kill_switch": {
                    "boj_eve": {"validator_callable_id": "boj_eve_window"},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    events_ref = "records/_calendars/events/test.yaml"
    events = tmp_path / events_ref
    events.parent.mkdir(parents=True)
    events.write_text(
        yaml.safe_dump(
            {
                "events": [
                    {
                        "event_id": "boj-20260506",
                        "date": "2026-05-06",
                        "kind": "boj",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    front = _trade_front(
        policy_ref=_snapshot(policy_ref),
        calendar_refs={
            "business_days": _snapshot("records/_calendars/business-days/2026-05.yaml"),
            "events": _snapshot(events_ref),
            "corporate_actions": _snapshot("records/_calendars/corporate-actions/2026-05.yaml"),
        },
        kill_switch_check={"boj_eve": False},
    )
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.kill-switch-check" in codes


def test_no_margin_trading_constraint_rejects_margin_usage(tmp_path: Path) -> None:
    front = _trade_front()
    intent = front["order_intent"]
    checked = front["kill_switch_check"]
    assert isinstance(intent, dict)
    assert isinstance(checked, dict)
    intent["uses_margin"] = True
    checked["no_margin_trading"] = True
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.no-margin-trading" in codes


def test_filename_ticker_must_match_front_matter(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_front(ticker="1111"))
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.filename-ticker" in codes


def test_invalid_ticker_pattern_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_front(ticker="bad"), name="2026-05-05-bad.md")
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.pattern" in codes
    assert "trade.ticker-format" in codes


def test_discover_trade_files_skips_template(tmp_path: Path) -> None:
    trade_root = tmp_path / "records/06-trades"
    trade_root.mkdir(parents=True)
    (trade_root / "template.md").write_text("placeholder", encoding="utf-8")
    valid = _write_trade(tmp_path)
    discovered = discover_trade_files(trade_root)
    assert discovered == [valid]
