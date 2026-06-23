from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

from baibai_loop.position.review import due_review_gates, weekday_calendar
from baibai_loop.position.trades import TradeRecord, load_open_trades


def _trade(
    *,
    ticker: str = "9682",
    entry_date: date = date(2026, 5, 7),
    review_state: str = "scheduled",
) -> TradeRecord:
    return TradeRecord(
        trade_id=f"trade-{ticker}",
        ticker=ticker,
        name="Sample",
        position_state="open",
        review_state=review_state,
        entry_date=entry_date,
        quantity=100,
        entry_price=1000.0,
    )


def test_due_review_gates_returns_passed_targets_for_scheduled_trade() -> None:
    calendar = weekday_calendar(date(2026, 5, 1), date(2026, 7, 31))
    gates = due_review_gates([_trade()], date(2026, 6, 6), calendar)
    assert [gate.horizon for gate in gates] == ["plus_15bd"]


def test_due_review_gates_excludes_future_target() -> None:
    calendar = weekday_calendar(date(2026, 5, 1), date(2026, 7, 31))
    # +15bd from 2026-05-25 lands 2026-06-15, after the asof.
    gates = due_review_gates([_trade(entry_date=date(2026, 5, 25))], date(2026, 6, 6), calendar)
    assert gates == []


def test_due_review_gates_skips_completed_review() -> None:
    calendar = weekday_calendar(date(2026, 5, 1), date(2026, 7, 31))
    gates = due_review_gates([_trade(review_state="completed")], date(2026, 7, 31), calendar)
    assert gates == []


def test_due_review_gates_reports_both_horizons_when_due() -> None:
    calendar = weekday_calendar(date(2026, 5, 1), date(2026, 7, 31))
    gates = due_review_gates([_trade()], date(2026, 7, 31), calendar)
    assert [gate.horizon for gate in gates] == ["plus_15bd", "plus_30bd"]


def test_load_open_trades_parses_executions(tmp_path: Path) -> None:
    trade_dir = tmp_path / "records/06-trades/2026/05"
    trade_dir.mkdir(parents=True)
    (trade_dir / "2026-05-07-9682.md").write_text(
        textwrap.dedent(
            """\
            ---
            trade_id: trade-20260505-9682
            ticker: '9682'
            name: Sample
            position_state: open
            review_state: scheduled
            executions:
            - side: buy
              quantity: 200
              price_yen: 1010
              at: '2026-05-07T09:00:00+09:00'
            ---

            body
            """
        ),
        encoding="utf-8",
    )
    trades = load_open_trades(tmp_path)
    assert len(trades) == 1
    assert trades[0].entry_date == date(2026, 5, 7)
    assert trades[0].quantity == 200
    assert trades[0].entry_price == 1010.0


def test_load_open_trades_skips_closed_position(tmp_path: Path) -> None:
    trade_dir = tmp_path / "records/06-trades/2026/05"
    trade_dir.mkdir(parents=True)
    (trade_dir / "closed.md").write_text(
        textwrap.dedent(
            """\
            ---
            trade_id: trade-closed
            ticker: '9999'
            position_state: closed
            review_state: completed
            executions:
            - side: buy
              quantity: 100
              price_yen: 500
              at: '2026-05-07T09:00:00+09:00'
            ---
            """
        ),
        encoding="utf-8",
    )
    assert load_open_trades(tmp_path) == []
