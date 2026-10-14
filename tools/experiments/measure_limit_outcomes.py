"""Describe recorded buy-order outcomes without inferring fills or policy effects.

台帳上の買付予約・約定・解放を集計し、失効後の観測終値と指値の差を補足する。
前日終値固定の旧ルールやProposalの承認を参照IDだけから推定しない。
価格差は実現できた利益・因果効果ではなく、株式数basisを確認できる場合の記述統計。
read-only。asofはJST日付で、ledgerとmarket storeを書き換えない。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite
from pathlib import Path
from statistics import median
from typing import TextIO

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerError,
    ReleaseEvent,
    ReservationEvent,
)
from baibai_engine.position.market_source import quantity_basis_is_confirmed
from baibai_engine.position.store import LedgerStoreService

DEFAULT_APP_DB = Path("stores/application/baibai.sqlite")
DEFAULT_MARKET_DB = Path("stores/market/market.sqlite")
# 失効後にどこまで見て「逃した幅」と呼ぶか。決算 1 本を挟む程度の窓にする。
POST_EXPIRY_SESSIONS = 20


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
    price_comparison_unavailable_reason: str | None = None


def _bars(
    connection: sqlite3.Connection, ticker: str, start: date, end: date
) -> list[tuple[date, float, float]]:
    rows = connection.execute(
        "SELECT traded_at, low, close FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at BETWEEN ? AND ? "
        "AND low IS NOT NULL AND close IS NOT NULL ORDER BY traded_at",
        (ticker, start.isoformat(), end.isoformat()),
    ).fetchall()
    values = [(date.fromisoformat(str(row[0])), float(row[1]), float(row[2])) for row in rows]
    if any(not isfinite(value) or value <= 0 for _, low, close in values for value in (low, close)):
        raise LimitOutcomeMeasurementError("invalid observed order-window price")
    return values


def _forward_closes(
    connection: sqlite3.Connection, ticker: str, after: date, asof: date, sessions: int
) -> list[tuple[date, float]]:
    rows = connection.execute(
        "SELECT traded_at, close FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at > ? AND traded_at <= ? AND close IS NOT NULL "
        "ORDER BY traded_at LIMIT ?",
        (ticker, after.isoformat(), asof.isoformat(), sessions),
    ).fetchall()
    values = [(date.fromisoformat(str(row[0])), float(row[1])) for row in rows]
    if any(not isfinite(close) or close <= 0 for _, close in values):
        raise LimitOutcomeMeasurementError("invalid observed post-expiry price")
    return values


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
        if event.occurred_at.astimezone(JST).date() > asof:
            continue
        if isinstance(event, ReservationEvent) and event.side == "buy":
            reservations[event.reservation_id] = event
        elif isinstance(event, ReleaseEvent):
            releases[event.reservation_id] = event
        elif isinstance(event, ExecutionEvent) and event.reservation_id is not None:
            fills.setdefault(event.reservation_id, []).append(event)

    connection = sqlite3.connect(f"{market_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        outcomes = [
            _order_outcome(
                connection,
                market_db=market_db,
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

    # 参照IDの有無を分けるだけで、Proposal由来や承認済みとは推定しない。
    decision_bound = [item for item in outcomes if item.decision_reference is not None]
    return {
        "kind": "limit-outcome-aggregate",
        "asof": asof.isoformat(),
        "scope": "buy_reservations",
        "interpretation": (
            "報告済み注文状態と価格の記述統計。価格窓は取得済み終値の先頭20観測で、"
            "欠測のない20営業日、実現可能利益、指値方針の因果効果を証明しない。"
            "参照IDがあるだけでProposalの承認や特定の注文規律に従ったとはみなさない。"
        ),
        "ledger_as_of": ledger.as_of.isoformat(),
        "post_expiry_sessions": POST_EXPIRY_SESSIONS,
        "orders": [_order_payload(item) for item in outcomes],
        "summary": {
            "all_ledger_orders": _summarize(outcomes),
            "decision_bound_orders": _summarize(decision_bound),
        },
    }


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
        "forgone_pending_window_orders": sum(
            item.post_expiry_sessions_observed < POST_EXPIRY_SESSIONS
            and item.price_comparison_unavailable_reason is None
            for item in unfilled
        ),
        "price_incomparable_orders": sum(
            item.price_comparison_unavailable_reason is not None for item in unfilled
        ),
        "median_forgone_pct": None if not forgone else round(median(forgone), 2),
    }


def _order_outcome(
    connection: sqlite3.Connection,
    *,
    market_db: Path,
    reservation: ReservationEvent,
    release: ReleaseEvent | None,
    fills: Sequence[ExecutionEvent],
    asof: date,
) -> OrderOutcome:
    placed_on = reservation.occurred_at.astimezone(JST).date()
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
            release.occurred_at.astimezone(JST).date()
            if release is not None
            else max(fill.occurred_at.astimezone(JST).date() for fill in fills)
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
    through_date = forward[-1][0] if forward else bars[-1][0] if bars else None
    comparable = through_date is not None and quantity_basis_is_confirmed(
        sqlite_path=market_db,
        ticker=reservation.ticker,
        from_date=placed_on,
        through_date=through_date,
        connection=connection,
    )
    if not comparable:
        window_low = None
    complete_window = len(forward) == POST_EXPIRY_SESSIONS
    high_close = max(close for _, close in forward) if complete_window and comparable else None
    return OrderOutcome(
        reservation_id=reservation.reservation_id,
        ticker=reservation.ticker,
        placed_on=placed_on,
        limit_yen=reservation.price_guard_yen,
        quantity=reservation.quantity,
        outcome="expired",
        price_comparison_unavailable_reason=None if comparable else "share_basis_unconfirmed",
        filled_quantity=filled_quantity,
        remaining_quantity=0,
        release_reason=release.reason,
        decision_reference=reservation.decision_reference,
        window_end=window_end,
        window_low_yen=window_low,
        # 観測安値と指値の差。日中の順序・注文queue・実際の約定可能性は推定しない。
        distance_to_limit_pct=(
            None if window_low is None else _pct(window_low, reservation.price_guard_yen)
        ),
        post_expiry_sessions_observed=len(forward),
        post_window_high_close_yen=high_close,
        # 指値に対する観測最高終値の差。後知恵の比較であり逸失利益ではない。
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
        "price_comparison_unavailable_reason": item.price_comparison_unavailable_reason,
    }


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.experiments.measure_limit_outcomes",
        description=(
            "Describe reported buy-order states and observed price differences. "
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
