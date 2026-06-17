from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.validate.trade import discover_trade_files, validate_trade_file


def _snapshot(ref_path: str) -> dict[str, object]:
    return {"ref_path": ref_path}


def _trade_front(**overrides: object) -> dict[str, object]:
    front: dict[str, object] = {
        "trade_id": "trade-20260505-9682",
        "ticker": "9682",
        "playbook_id": "sales-discount-growth",
        "research_ref": "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md",
        "position_state": "open",
        "current_quantity": 200,
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
                "order_price_guard_yen": 1050,
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
    research_path = root / "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md"
    research_path.parent.mkdir(parents=True, exist_ok=True)
    if not research_path.exists():
        research_path.write_text(
            "---\n"
            "research_decision:\n"
            "  outcome: approved\n"
            "  posture: act_now\n"
            "sector_33: 情報・通信業\n"
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
    assert "trade.position-sizing-required" in codes


def test_submitted_trade_schema_requires_sizing_fields(tmp_path: Path) -> None:
    front = _trade_front()
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    del sizing["guarded_max_notional_yen"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.position-sizing-field" in codes


def test_trade_nested_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    front = _trade_front()
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    sizing["extra_size_field"] = 1
    orders = front["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    order["extra_order_field"] = "unexpected"
    executions = front["executions"]
    assert isinstance(executions, list)
    execution = executions[0]
    assert isinstance(execution, dict)
    execution["extra_execution_field"] = "unexpected"

    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.additionalProperties" in codes


def test_filled_trade_requires_execution_fields(tmp_path: Path) -> None:
    front = _trade_front()
    executions = front["executions"]
    assert isinstance(executions, list)
    execution = executions[0]
    assert isinstance(execution, dict)
    del execution["at"]
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.required" in codes


def test_execution_order_id_must_join_to_order(tmp_path: Path) -> None:
    front = _trade_front()
    executions = front["executions"]
    assert isinstance(executions, list)
    execution = executions[0]
    assert isinstance(execution, dict)
    execution["order_id"] = "order-other"
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.execution-order-join" in codes


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
    root = _test_repo_root(tmp_path)
    research_path = root / "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md"
    research_path.parent.mkdir(parents=True, exist_ok=True)
    research_path.write_text(
        "---\n"
        "research_decision:\n"
        "  outcome: deferred\n"
        "  posture: wait_for_event\n"
        "---\n\n# Research\n",
        encoding="utf-8",
    )
    path = _write_trade(tmp_path)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.research-approval" in codes


def test_submitted_trade_requires_readable_research_ref(tmp_path: Path) -> None:
    front = _trade_front(research_ref="records/05-research/missing.md")
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.research-ref-load" in codes


def test_no_margin_trading_constraint_rejects_margin_usage(tmp_path: Path) -> None:
    front = _trade_front()
    intent = front["order_intent"]
    assert isinstance(intent, dict)
    intent["uses_margin"] = True
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.no-margin-trading" in codes


def test_open_trades_must_stay_within_portfolio_concentration_caps(tmp_path: Path) -> None:
    front = _trade_front(current_quantity=3000)
    front["position_sizing_overlay"] = {
        "estimated_real_order_notional_yen": 3150000,
        "guarded_max_notional_yen": 3150000,
    }
    intent = front["order_intent"]
    assert isinstance(intent, dict)
    front["order_intent"] = {**intent, "quantity": 3000, "order_price_guard_yen": 1050}
    front["entry_legs"] = [
        {
            "entry_leg_id": "entry-20260505-9682-1",
            "research_ref": "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md",
            "order_id": "order-20260505-9682-entry",
            "quantity": 3000,
            "average_price_yen": 1050,
        }
    ]
    path = _write_trade(tmp_path, front)

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.portfolio-tactical-budget" in codes
    assert "trade.portfolio-ticker-cap" in codes
    assert "trade.portfolio-sector-cap" in codes
    assert "trade.portfolio-playbook-cap" in codes


def test_closed_trade_does_not_report_current_portfolio_concentration_caps(
    tmp_path: Path,
) -> None:
    path = _write_trade(
        tmp_path,
        _trade_front(
            position_state="closed",
            trade_execution_state="filled",
            current_quantity=0,
        ),
    )

    codes = {finding.code for finding in validate_trade_file(path)}

    assert "trade.portfolio-tactical-budget" not in codes
    assert "trade.portfolio-ticker-cap" not in codes


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
