from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.validate.trade import discover_trade_files, validate_trade_file

_DIGEST = "sha256:" + "a" * 64


def _snapshot(ref_path: str) -> dict[str, object]:
    return {"ref_path": ref_path, "content_sha256": _DIGEST}


def _trade_front(**overrides: object) -> dict[str, object]:
    front: dict[str, object] = {
        "trade_id": "trade-20260505-9682",
        "ticker": "9682",
        "research_ref": "records/05-research/2026/05/2026-05-05-9682-sales-discount-growth.md",
        "policy_snapshot": _snapshot(
            "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
        ),
        "portfolio_exposure_snapshot_ref": _snapshot(
            "records/_portfolio-exposure/2026/05/2026-05-05T200000+0900.yaml"
        ),
        "position_state": "open",
        "review_state": "not_due",
        "trade_execution_state": "filled",
        "order_intent": {
            "order_intent_id": "intent-20260505-9682-entry",
            "quantity": 200,
            "order_price_guard_yen": 1050,
            "not_submitted_reason": None,
        },
        "position_sizing_overlay": {
            "guarded_max_notional_yen": 210000,
            "estimated_real_order_notional_yen": 210000,
        },
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
    }
    front.update(overrides)
    return front


def _write_trade(
    tmp_path: Path, front: dict[str, object] | None = None, name: str = "2026-05-05-9682.md"
) -> Path:
    path = tmp_path / name
    payload = front if front is not None else _trade_front()
    path.write_text(
        "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) + "---\n\n# Trade\n",
        encoding="utf-8",
    )
    return path


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


def test_guarded_notional_must_match_quantity_times_guard(tmp_path: Path) -> None:
    front = _trade_front()
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    sizing["guarded_max_notional_yen"] = 202800
    path = _write_trade(tmp_path, front)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.guarded-notional" in codes


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
    (tmp_path / "template.md").write_text("placeholder", encoding="utf-8")
    valid = _write_trade(tmp_path)
    discovered = discover_trade_files(tmp_path)
    assert discovered == [valid]
