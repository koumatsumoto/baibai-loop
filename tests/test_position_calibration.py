from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from baibai_loop.market.sqlite import open_connection
from baibai_loop.position.cli import main


def test_calibration_cli_emits_fv_gap_zone_action_and_returns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_thesis(tmp_path, "1111", fair_value_yen=1200, expected_upside_pct=20.0)
    _write_position(
        tmp_path,
        "1111",
        quantity=100,
        entry_price=1000,
        entry_expected_upside_pct=20.0,
        entry_expected_yield_pct=5.0,
    )
    _seed_bars(
        tmp_path,
        {
            "1111": {date(2026, 1, 11): 1000, date(2026, 1, 15): 1330},
            "1321": {date(2026, 1, 11): 1000, date(2026, 1, 15): 1100},
        },
    )

    assert main(["calibration", "--root", str(tmp_path), "--asof", "2026-01-15"]) == 0

    payload = yaml.safe_load(capsys.readouterr().out)
    row = payload["positions"][0]
    assert row["ticker"] == "1111"
    assert row["name"] == "Sample 1111"
    assert row["entry_date"] == "2026-01-11"
    assert row["quantity"] == 100
    assert row["entry_price"] == 1000.0
    assert row["entry_expected_upside_pct"] == 20.0
    assert row["entry_expected_yield_pct"] == 5.0
    assert row["thesis_fair_value_yen"] == 1200.0
    assert row["current_price_yen"] == 1330.0
    assert row["current_return_pct"] == 33.0
    assert row["benchmark_return_pct"] == 10.0
    assert row["relative_return_pct"] == 23.0
    assert row["fv_gap_pct"] == pytest.approx(10.8333)
    assert row["valuation_zone"] == "rich"
    # Reaching fair value is a review trigger, not an auto-sell.
    assert row["review_trigger"] is True
    assert payload["aggregate"]["position_count"] == 1
    assert payload["aggregate"]["review_trigger_count"] == 1
    assert payload["coverage"]["current_price_count"] == 1
    assert payload["coverage"]["thesis_fair_value_count"] == 1
    assert "not an automatic sell" in payload["note"]


def test_calibration_review_trigger_starts_at_fair_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_thesis(tmp_path, "4444", fair_value_yen=1000, expected_upside_pct=10.0)
    _write_position(
        tmp_path,
        "4444",
        quantity=100,
        entry_price=900,
        entry_expected_upside_pct=10.0,
        entry_expected_yield_pct=None,
    )
    _seed_bars(
        tmp_path,
        {
            "4444": {date(2026, 1, 11): 900, date(2026, 1, 15): 1000},
            "1321": {date(2026, 1, 11): 1000, date(2026, 1, 15): 1000},
        },
    )

    assert main(["calibration", "--root", str(tmp_path), "--asof", "2026-01-15"]) == 0

    payload = yaml.safe_load(capsys.readouterr().out)
    row = payload["positions"][0]
    assert row["valuation_zone"] == "fair"
    assert row["review_trigger"] is True


def test_calibration_cli_degrades_to_null_when_fv_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_thesis(tmp_path, "2222", fair_value_yen=None, expected_upside_pct=None)
    _write_position(
        tmp_path,
        "2222",
        quantity=100,
        entry_price=1000,
        entry_expected_upside_pct=None,
        entry_expected_yield_pct=None,
    )
    _seed_bars(
        tmp_path,
        {
            "2222": {date(2026, 1, 11): 1000, date(2026, 1, 15): 900},
            "1321": {date(2026, 1, 11): 1000, date(2026, 1, 15): 950},
        },
    )

    assert main(["calibration", "--root", str(tmp_path), "--asof", "2026-01-15"]) == 0

    row = yaml.safe_load(capsys.readouterr().out)["positions"][0]
    assert row["current_price_yen"] == 900.0
    assert row["current_return_pct"] == -10.0
    assert row["thesis_fair_value_yen"] is None
    assert row["fv_gap_pct"] is None
    assert row["valuation_zone"] is None
    assert row["review_trigger"] is None


def test_calibration_cli_degrades_prices_to_null_without_jquants_cache(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_thesis(tmp_path, "3333", fair_value_yen=1000, expected_upside_pct=20.0)
    _write_position(
        tmp_path,
        "3333",
        quantity=100,
        entry_price=800,
        entry_expected_upside_pct=20.0,
        entry_expected_yield_pct=None,
    )

    assert main(["calibration", "--root", str(tmp_path), "--asof", "2026-01-15"]) == 0

    captured = capsys.readouterr()
    payload = yaml.safe_load(captured.out)
    row = payload["positions"][0]
    assert row["current_price_yen"] is None
    assert row["current_return_pct"] is None
    assert row["benchmark_return_pct"] is None
    assert row["relative_return_pct"] is None
    assert row["fv_gap_pct"] is None
    assert row["valuation_zone"] is None
    assert row["review_trigger"] is None
    assert payload["coverage"]["jquants_bars_loaded"] is False
    assert payload["coverage"]["warnings_count"] == len(payload["warnings"])
    assert "no J-Quants bars" in captured.err


def test_legacy_benchmark_subcommand_is_not_exposed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["benchmark", "--root", str(tmp_path), "--asof", "2026-01-15"])


def _write_thesis(
    root: Path,
    ticker: str,
    *,
    fair_value_yen: float | None,
    expected_upside_pct: float | None,
) -> None:
    path = root / f"records/03-thesis/2026/01/2026-01-10-{ticker}-sample.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "ticker": ticker,
        "name": f"Sample {ticker}",
        "thesis_payoff": {
            "max_entry_price_yen": 1000,
            "fair_value_yen": fair_value_yen,
            "expected_upside_pct": expected_upside_pct,
            "expected_yield_pct": None,
        },
    }
    path.write_text(_front_matter(payload), encoding="utf-8")


def _write_position(
    root: Path,
    ticker: str,
    *,
    quantity: int,
    entry_price: float,
    entry_expected_upside_pct: float | None,
    entry_expected_yield_pct: float | None,
) -> None:
    path = root / f"records/04-position/2026/01/2026-01-11-{ticker}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "position_id": f"trade-20260111-{ticker}",
        "ticker": ticker,
        "name": f"Sample {ticker}",
        "thesis_ref": f"records/03-thesis/2026/01/2026-01-10-{ticker}-sample.md",
        "position_state": "open",
        "current_quantity": quantity,
        "execution_state": "filled",
        "order_intent": {
            "order_intent_id": f"intent-20260111-{ticker}-buy",
            "decision_event_id": f"decision-20260111-{ticker}-trade",
            "side": "buy",
            "quantity": quantity,
            "order_price_guard_yen": entry_price,
            "uses_margin": False,
        },
        "orders": [
            {
                "order_id": f"order-20260111-{ticker}-buy-1",
                "origin_order_intent_id": f"intent-20260111-{ticker}-buy",
                "side": "buy",
                "state": "filled",
                "submitted_quantity": quantity,
                "filled_quantity": quantity,
                "order_price_guard_yen": entry_price,
            }
        ],
        "executions": [
            {
                "execution_id": f"exec-20260111-{ticker}-buy-1",
                "order_id": f"order-20260111-{ticker}-buy-1",
                "side": "buy",
                "quantity": quantity,
                "price_yen": entry_price,
                "at": "2026-01-11T09:00:00+09:00",
            }
        ],
        "estimate_calibration": {
            "entry_expected_upside_pct": entry_expected_upside_pct,
            "entry_expected_yield_pct": entry_expected_yield_pct,
            "realized_return_pct": None,
            "realized_yield_pct": None,
            "thesis_held": None,
        },
    }
    path.write_text(_front_matter(payload), encoding="utf-8")


def _seed_bars(root: Path, closes: dict[str, dict[date, float]]) -> None:
    sqlite_path = root / "data/screening/market.sqlite"
    conn = open_connection(sqlite_path)
    try:
        start = date(2026, 1, 1)
        end = date(2026, 1, 15)
        current = start
        while current <= end:
            for ticker, ticker_closes in closes.items():
                close = ticker_closes.get(current)
                if close is None:
                    previous_dates = [day for day in ticker_closes if day <= current]
                    close = ticker_closes[max(previous_dates)] if previous_dates else 1000.0
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (ticker, current.isoformat(), close, close, 1.0),
                )
            current += timedelta(days=1)
        conn.commit()
    finally:
        conn.close()


def _front_matter(payload: dict[str, object]) -> str:
    return (
        "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) + "---\n\n# Test\n"
    )
