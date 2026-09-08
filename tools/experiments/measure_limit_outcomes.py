"""Aggregate what the planning-limit discipline actually captured.

指値は前営業日 raw close 固定で、gap を追わない。この規律は安く買えた日を守る一方、
上昇局面では「人間が approve した買い」を 0 株にする。どちらが効いたかは注文ごとの
個票を並べないと分からないので、全数量約定率と、失効した注文の未約定残に対する価格の逸失幅を測る。

read-only。ledger も market store も書き換えず、約定を推定しない。
asof当日までに発生したledger eventと価格だけで指値規律の結果を測る。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import TextIO

import yaml

from baibai_engine.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerError,
    ReleaseEvent,
    ReservationEvent,
)
from baibai_engine.position.store import LedgerStoreService

DEFAULT_APP_DB = Path("stores/application/baibai.sqlite")
DEFAULT_MARKET_DB = Path("stores/market/market.sqlite")
# 失効後にどこまで見て「逃した幅」と呼ぶか。決算 1 本を挟む程度の窓にする。
POST_EXPIRY_SESSIONS = 20
# chase policy — gap を追う指値へ変えるか — を判断するのに要る観測数。これに満たない間は
# 個票を並べるだけにして、方針変更の根拠にしない。
CHASE_POLICY_DECISION_MIN_ORDERS = 8


class LimitOutcomeMeasurementError(ValueError):
    """Raised when the stores cannot support a truthful order-by-order view."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderOutcome:
    reservation_id: str
    ticker: str
    placed_on: date
    limit_yen: Decimal
    quantity: int
    outcome: str
    filled_quantity: int
    remaining_quantity: int
    release_reason: str | None
    decision_reference: str | None
    window_end: date | None
    window_low_yen: float | None
    distance_to_limit_pct: float | None
    # 失効後に実際に観測できた立会日数。`forgone_pct` は窓が満ちた注文にだけ付く。
    # 4 日ぶんの上昇を 20 日窓の「逃した幅」として出すと、まだ測っていないものが
    # 測り終えた値の顔で中央値へ入る。
    post_expiry_sessions_observed: int
    post_window_high_close_yen: float | None
    forgone_pct: float | None


def _bars(
    connection: sqlite3.Connection, ticker: str, start: date, end: date
) -> list[tuple[date, float, float]]:
    rows = connection.execute(
        "SELECT traded_at, low, close FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at BETWEEN ? AND ? "
        "AND low IS NOT NULL AND close IS NOT NULL ORDER BY traded_at",
        (ticker, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [(date.fromisoformat(str(row[0])), float(row[1]), float(row[2])) for row in rows]


def _forward_closes(
    connection: sqlite3.Connection, ticker: str, after: date, asof: date, sessions: int
) -> list[float]:
    rows = connection.execute(
        "SELECT close FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at > ? AND traded_at <= ? AND close IS NOT NULL "
        "ORDER BY traded_at LIMIT ?",
        (ticker, after.isoformat(), asof.isoformat(), sessions),
    ).fetchall()
    return [float(row[0]) for row in rows]


def _pct(numerator: Decimal | float, denominator: Decimal | float) -> float:
    return round((float(numerator) / float(denominator) - 1.0) * 100, 2)


def build_limit_outcomes(*, app_db: Path, market_db: Path, asof: date) -> dict[str, object]:
    try:
        ledger = LedgerStoreService(app_db).load()
    except (PortfolioLedgerError, OSError, ValueError) as error:
        raise LimitOutcomeMeasurementError(f"cannot read the ledger: {error}") from error
    if not market_db.exists():
        raise LimitOutcomeMeasurementError(f"market store is unavailable: {market_db}")

    reservations: dict[str, ReservationEvent] = {}
    releases: dict[str, ReleaseEvent] = {}
    fills: dict[str, list[ExecutionEvent]] = {}
    for event in ledger.events:
        if event.occurred_at.date() > asof:
            continue
        if isinstance(event, ReservationEvent):
            reservations[event.reservation_id] = event
        elif isinstance(event, ReleaseEvent):
            releases[event.reservation_id] = event
        elif isinstance(event, ExecutionEvent) and event.reservation_id is not None:
            fills.setdefault(event.reservation_id, []).append(event)

    connection = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        outcomes = [
            _order_outcome(
                connection,
                reservation=reservation,
                release=releases.get(reservation_id),
                fills=fills.get(reservation_id, []),
                asof=asof,
            )
            for reservation_id, reservation in sorted(
                reservations.items(), key=lambda item: item[1].occurred_at
            )
        ]
    finally:
        connection.close()

    # decision reference を持つ注文だけが repository の判断経路を通っている。持たない行は
    # 既存保有の取り込みで、その約定は指値規律の結果ではない。混ぜて 1 つの fill 率にすると
    # 取り込み分が規律の成績に化ける。
    decision_bound = [item for item in outcomes if item.decision_reference is not None]
    return {
        "kind": "limit-outcome-aggregate",
        "asof": asof.isoformat(),
        "ledger_as_of": ledger.as_of.isoformat(),
        "post_expiry_sessions": POST_EXPIRY_SESSIONS,
        "orders": [_order_payload(item) for item in outcomes],
        "summary": {
            "all_ledger_orders": _summarize(outcomes),
            "decision_bound_orders": _summarize(decision_bound),
        },
        "chase_policy_decision": {
            "min_orders": CHASE_POLICY_DECISION_MIN_ORDERS,
            "decided_orders": _decided_count(decision_bound),
            "ready": _decided_count(decision_bound) >= CHASE_POLICY_DECISION_MIN_ORDERS,
            "note": (
                "判断参照付きの全数量約定＋報告済み失効だけを数える。取消等と継続中は除く。"
                "gap を追う指値へ変えるかは、この件数に達してから別 issue で事前登録して"
                "判断する。少数の失効だけを見て規律を外さない。"
            ),
        },
    }


def _decided_count(outcomes: Sequence[OrderOutcome]) -> int:
    return sum(1 for item in outcomes if item.outcome in {"filled", "expired"})


def _summarize(outcomes: Sequence[OrderOutcome]) -> dict[str, object]:
    filled = [item for item in outcomes if item.outcome == "filled"]
    unfilled = [item for item in outcomes if item.outcome == "expired"]
    distances = [
        item.distance_to_limit_pct for item in unfilled if item.distance_to_limit_pct is not None
    ]
    forgone = [item.forgone_pct for item in unfilled if item.forgone_pct is not None]
    decided = len(filled) + len(unfilled)
    return {
        "orders": len(outcomes),
        "filled": len(filled),
        "expired": len(unfilled),
        "cancelled": sum(item.outcome == "cancelled" for item in outcomes),
        "broker_rejected": sum(item.outcome == "broker_rejected" for item in outcomes),
        "decision_changed": sum(item.outcome == "decision_changed" for item in outcomes),
        "still_open": sum(item.outcome in {"open", "partially_filled"} for item in outcomes),
        "partially_filled_open": sum(item.outcome == "partially_filled" for item in outcomes),
        "decided_orders": decided,
        "full_fill_rate_pct": None if not decided else round(len(filled) / decided * 100, 1),
        "median_low_above_limit_pct": None if not distances else round(median(distances), 2),
        # 中央値の母数は窓が満ちた失効注文だけ。まだ窓の途中にある注文をここへ入れると、
        # 直近の失効ほど「逃した幅が小さい」側へ寄る。
        "forgone_measured_orders": len(forgone),
        "forgone_pending_window_orders": len(unfilled) - len(forgone),
        "median_forgone_pct": None if not forgone else round(median(forgone), 2),
    }


def _order_outcome(
    connection: sqlite3.Connection,
    *,
    reservation: ReservationEvent,
    release: ReleaseEvent | None,
    fills: Sequence[ExecutionEvent],
    asof: date,
) -> OrderOutcome:
    placed_on = reservation.occurred_at.date()
    filled_quantity = sum(fill.quantity for fill in fills)
    unfilled_quantity = reservation.quantity - filled_quantity
    outcome = (
        release.reason
        if release is not None
        else "filled"
        if unfilled_quantity == 0
        else "partially_filled"
        if filled_quantity
        else "open"
    )
    remaining_quantity = unfilled_quantity if release is None else 0
    if outcome != "expired":
        window_end = (
            release.occurred_at.date()
            if release is not None
            else max(fill.occurred_at.date() for fill in fills)
            if outcome == "filled"
            else None
        )
        return OrderOutcome(
            reservation_id=reservation.reservation_id,
            ticker=reservation.ticker,
            placed_on=placed_on,
            limit_yen=reservation.price_guard_yen,
            quantity=reservation.quantity,
            outcome=outcome,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            release_reason=release.reason if release is not None else None,
            decision_reference=reservation.decision_reference,
            window_end=window_end,
            window_low_yen=None,
            distance_to_limit_pct=None,
            post_expiry_sessions_observed=0,
            post_window_high_close_yen=None,
            forgone_pct=None,
        )
    assert release is not None
    window_end = min(release.occurred_at.date(), asof)
    bars = _bars(connection, reservation.ticker, placed_on, window_end)
    window_low = min((low for _, low, _ in bars), default=None)
    forward = _forward_closes(
        connection, reservation.ticker, window_end, asof, POST_EXPIRY_SESSIONS
    )
    complete_window = len(forward) == POST_EXPIRY_SESSIONS
    high_close = max(forward, default=None) if complete_window else None
    return OrderOutcome(
        reservation_id=reservation.reservation_id,
        ticker=reservation.ticker,
        placed_on=placed_on,
        limit_yen=reservation.price_guard_yen,
        quantity=reservation.quantity,
        outcome="expired",
        filled_quantity=filled_quantity,
        remaining_quantity=0,
        release_reason=release.reason,
        decision_reference=reservation.decision_reference,
        window_end=window_end,
        window_low_yen=window_low,
        # 窓内の最安値が指値をどれだけ上回ったか。0 に近いほど「あと少しで約定した」。
        distance_to_limit_pct=(
            None if window_low is None else _pct(window_low, reservation.price_guard_yen)
        ),
        post_expiry_sessions_observed=len(forward),
        post_window_high_close_yen=high_close,
        # 失効後に届かなかった上昇幅。指値規律のコスト側で、fill 率と対で読む。
        forgone_pct=(None if high_close is None else _pct(high_close, reservation.price_guard_yen)),
    )


def _order_payload(item: OrderOutcome) -> dict[str, object]:
    return {
        "reservation_id": item.reservation_id,
        "ticker": item.ticker,
        "placed_on": item.placed_on.isoformat(),
        "limit_yen": float(item.limit_yen),
        "quantity": item.quantity,
        "outcome": item.outcome,
        "filled_quantity": item.filled_quantity,
        "unfilled_quantity": item.quantity - item.filled_quantity,
        "remaining_quantity": item.remaining_quantity,
        "filled_quantity_pct": round(item.filled_quantity / item.quantity * 100, 1),
        "release_reason": item.release_reason,
        "decision_reference": item.decision_reference,
        "window_end": None if item.window_end is None else item.window_end.isoformat(),
        "window_low_yen": item.window_low_yen,
        "distance_to_limit_pct": item.distance_to_limit_pct,
        "post_expiry_sessions_observed": item.post_expiry_sessions_observed,
        "post_window_high_close_yen": item.post_window_high_close_yen,
        "forgone_pct": item.forgone_pct,
    }


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.measure_limit_outcomes",
        description=(
            "Aggregate full-quantity fill rate and forgone upside across "
            "human-approved limit orders. "
            "Read-only: never infers a fill and never writes canonical records."
        ),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_APP_DB)
    parser.add_argument("--sqlite-path", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument(
        "--asof",
        type=_parse_date,
        required=True,
        help="inclusive observation cutoff for ledger events and market bars",
    )
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        payload = build_limit_outcomes(app_db=args.db, market_db=args.sqlite_path, asof=args.asof)
    except (LimitOutcomeMeasurementError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, file=stdout or sys.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
