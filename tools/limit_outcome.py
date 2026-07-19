"""Read-only individual observation for one human-confirmed expired limit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_engine.market.sqlite import (
    SQLiteSchemaError,
    optional_float,
    range_covered,
    validate_current_schema,
)
from baibai_engine.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    ReleaseEvent,
    ReservationEvent,
    load_portfolio_ledger_with_sha256,
    replay_events_through,
)

_JST = ZoneInfo("Asia/Tokyo")
_JPX_SESSION_CLOSE = time(15, 30)
_HORIZON_SESSIONS = 5
_MARKET_HASH_BASIS = (
    "canonical-json-v1:jquants_market_calendar(day,is_business_day)"
    "+jquants_daily_bars(ticker,traded_at,raw_low,raw_close,adjustment_factor)"
)


class _LimitOutcomeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _RawBar:
    ticker: str
    traded_at: date
    low: float | None
    close: float | None
    adjustment_factor: float | None


@dataclass(frozen=True, slots=True)
class _MarketSnapshot:
    calendar: tuple[tuple[date, bool], ...]
    bars: tuple[_RawBar, ...]
    fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class _ObservationTarget:
    reservation: ReservationEvent
    submission_date: date
    expiry_date: date
    remaining_quantity: int
    asof: date


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.limit_outcome",
        description=(
            "Emit operation-Issue YAML with raw daily-low touch facts and post-expiry "
            "price observations. Never infer a fill or write canonical records."
        ),
    )
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--reservation-id", required=True)
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=DEFAULT_SQLITE_CACHE_DIR / "market.sqlite",
    )
    parser.add_argument("--asof", type=_date_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger_path = args.ledger
    sqlite_path = args.sqlite_path
    market_fingerprint: str | None = None
    try:
        ledger, ledger_sha256 = load_portfolio_ledger_with_sha256(ledger_path)
        target, terminal = _evaluate_ledger_state(
            ledger,
            reservation_id=args.reservation_id,
            asof=args.asof,
        )
        if terminal is not None:
            payload = _result(target, *terminal)
        else:
            snapshot = _read_market_snapshot(
                sqlite_path,
                ticker=target.reservation.ticker,
                start=target.submission_date,
                expiry=target.expiry_date,
                asof=target.asof,
            )
            if snapshot is None:
                payload = _result(
                    target,
                    "unresolved",
                    "market_calendar_unavailable",
                )
            else:
                market_fingerprint = snapshot.fingerprint_sha256
                payload = _observe(target, snapshot)
    except (PortfolioLedgerError, _LimitOutcomeError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    payload["sources"] = {
        "ledger_ref": str(ledger_path),
        "ledger_sha256": ledger_sha256,
        "market_data_ref": str(sqlite_path),
        "selected_rows_sha256": market_fingerprint,
        "market_hash_basis": _MARKET_HASH_BASIS,
    }
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def _evaluate_ledger_state(
    document: PortfolioLedgerDocument,
    *,
    reservation_id: str,
    asof: date,
) -> tuple[_ObservationTarget, tuple[str, str] | None]:
    replay_events_through(document.events, document.as_of)
    reservation = _reservation(document, reservation_id)
    release = _release(document, reservation_id)
    submission_date = reservation.occurred_at.astimezone(_JST).date()
    expiry_date = reservation.expires_at.astimezone(_JST).date()
    if asof < submission_date:
        raise _LimitOutcomeError("asof must not predate reservation submission")
    if release is not None and asof < release.occurred_at.astimezone(_JST).date():
        raise _LimitOutcomeError("asof must not predate human-confirmed release")
    filled_quantity = sum(
        event.quantity
        for event in document.events
        if isinstance(event, ExecutionEvent)
        and event.reservation_id == reservation_id
        and event.side == "buy"
    )
    target = _ObservationTarget(
        reservation=reservation,
        submission_date=submission_date,
        expiry_date=expiry_date,
        remaining_quantity=reservation.quantity - filled_quantity,
        asof=asof,
    )
    if target.remaining_quantity <= 0:
        return target, ("not_eligible", "fully_filled")
    if release is None:
        return target, ("pending", "human_confirmed_release_required")
    if release.reason != "expired":
        return target, ("not_eligible", f"release_reason_{release.reason}")
    if asof < expiry_date:
        raise _LimitOutcomeError("asof must not predate expiry")
    return target, None


def _read_market_snapshot(
    sqlite_path: Path,
    *,
    ticker: str,
    start: date,
    expiry: date,
    asof: date,
) -> _MarketSnapshot | None:
    conn = _connect_read_only(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        calendar_rows = conn.execute(
            "SELECT day, is_business_day FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), asof.isoformat()),
        ).fetchall()
        available_calendar = tuple(
            (date.fromisoformat(str(day)), bool(is_business_day))
            for day, is_business_day in calendar_rows
        )
        post_expiry = tuple(
            day for day, is_business_day in available_calendar if is_business_day and day > expiry
        )
        selected_end = (
            post_expiry[_HORIZON_SESSIONS - 1] if len(post_expiry) >= _HORIZON_SESSIONS else asof
        )
        if not range_covered(conn, "jquants_market_calendar", start, selected_end):
            return None
        calendar = tuple(row for row in available_calendar if row[0] <= selected_end)
        bar_rows = conn.execute(
            "SELECT ticker, traded_at, low, close, adjustment_factor "
            "FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (ticker, start.isoformat(), selected_end.isoformat()),
        ).fetchall()
        bars = tuple(_raw_bar(row) for row in bar_rows)
        fingerprint_payload = {
            "hash_basis": _MARKET_HASH_BASIS,
            "ticker": ticker,
            "start": start.isoformat(),
            "end": selected_end.isoformat(),
            "calendar": [[day.isoformat(), flag] for day, flag in calendar],
            "bars": [
                [
                    bar.ticker,
                    bar.traded_at.isoformat(),
                    bar.low,
                    bar.close,
                    bar.adjustment_factor,
                ]
                for bar in bars
            ],
        }
        encoded = json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return _MarketSnapshot(
            calendar=calendar,
            bars=bars,
            fingerprint_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        )
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise _LimitOutcomeError(f"corrupt limit-outcome market snapshot: {error}") from error
    finally:
        conn.rollback()
        conn.close()


def _connect_read_only(sqlite_path: Path) -> sqlite3.Connection | None:
    if not sqlite_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
        validate_current_schema(conn)
    except (SQLiteSchemaError, sqlite3.Error):
        if conn is not None:
            conn.close()
        return None
    return conn


def _raw_bar(row: tuple[object, ...]) -> _RawBar:
    row_ticker, traded_at, low, close, adjustment_factor = row
    parsed = _RawBar(
        ticker=str(row_ticker),
        traded_at=date.fromisoformat(str(traded_at)),
        low=optional_float(low),
        close=optional_float(close),
        adjustment_factor=optional_float(adjustment_factor),
    )
    if any(
        value is not None and not isfinite(value)
        for value in (parsed.low, parsed.close, parsed.adjustment_factor)
    ):
        raise ValueError(f"non-finite raw bar for {parsed.ticker} on {parsed.traded_at}")
    return parsed


def _observe(target: _ObservationTarget, snapshot: _MarketSnapshot) -> dict[str, object]:
    reservation = target.reservation
    sessions = tuple(
        day for day, is_business_day in snapshot.calendar if is_business_day and day <= target.asof
    )
    session_set = set(sessions)
    eligible_sessions = tuple(
        day
        for day in sessions
        if day > target.submission_date
        and (
            day < target.expiry_date
            or (day == target.expiry_date and _expiry_covers_full_session(reservation))
        )
    )
    if not eligible_sessions:
        return _result(target, "unresolved", "no_observable_active_session")
    post_expiry_sessions = tuple(day for day in sessions if day > target.expiry_date)[
        :_HORIZON_SESSIONS
    ]
    reference_date = (
        next((day for day in reversed(sessions) if day < post_expiry_sessions[0]), None)
        if post_expiry_sessions
        else None
    )
    by_date = {bar.traded_at: bar for bar in snapshot.bars if bar.ticker == reservation.ticker}
    action_dates = set(eligible_sessions)
    if target.submission_date in session_set:
        action_dates.add(target.submission_date)
    if reference_date is not None:
        action_dates.add(reference_date)
    action_dates.update(post_expiry_sessions)
    checks = (
        (
            "corporate_action_in_observation_window",
            sorted(
                day
                for day in action_dates
                if day in by_date
                and by_date[day].adjustment_factor is not None
                and by_date[day].adjustment_factor != 1.0
            ),
        ),
        (
            "corporate_action_basis_unassessed",
            sorted(
                day
                for day in action_dates
                if day in by_date and by_date[day].adjustment_factor is None
            ),
        ),
        (
            "raw_bar_outside_business_calendar",
            sorted(
                day
                for day in by_date
                if target.submission_date <= day <= snapshot.calendar[-1][0]
                and day not in session_set
            ),
        ),
        (
            "missing_raw_low_for_active_session",
            sorted(
                day for day in eligible_sessions if day not in by_date or by_date[day].low is None
            ),
        ),
        (
            "missing_raw_bar_for_observation_window",
            sorted(day for day in action_dates if day not in by_date),
        ),
        (
            "invalid_raw_low_for_active_session",
            sorted(
                day
                for day in eligible_sessions
                if day in by_date and _is_nonpositive(by_date[day].low)
            ),
        ),
    )
    for reason, dates in checks:
        if dates:
            return _unresolved_for_dates(target, reason, dates)

    touched = [
        (day, by_date[day].low)
        for day in eligible_sessions
        if by_date[day].low is not None
        and Decimal(str(by_date[day].low)) <= reservation.price_guard_yen
    ]
    first_touch = touched[0] if touched else None
    payload = _result(target, "observed")
    payload["touch"] = {
        "touched_limit": bool(touched),
        "fill_inferred": False,
        "first_touch_date": first_touch[0].isoformat() if first_touch else None,
        "first_touch_raw_low_yen": first_touch[1] if first_touch else None,
        "touch_session_count": len(touched),
    }
    if len(post_expiry_sessions) < _HORIZON_SESSIONS:
        payload["status"] = "pending"
        payload["reason"] = "insufficient_post_expiry_sessions"
        payload["details"] = {"post_expiry_sessions_observed": len(post_expiry_sessions)}
        return payload

    assert reference_date is not None
    horizon_date = post_expiry_sessions[-1]
    reference_bar = by_date.get(reference_date)
    horizon_bar = by_date.get(horizon_date)
    close_pairs = ((reference_date, reference_bar), (horizon_date, horizon_bar))
    missing_close_dates = [day for day, bar in close_pairs if bar is None or bar.close is None]
    invalid_close_dates = [
        day for day, bar in close_pairs if bar is not None and _is_nonpositive(bar.close)
    ]
    if missing_close_dates or invalid_close_dates:
        reason = (
            "missing_raw_close_for_post_expiry_observation"
            if missing_close_dates
            else "invalid_raw_close_for_post_expiry_observation"
        )
        dates = missing_close_dates or invalid_close_dates
        return _unresolved_for_dates(target, reason, dates)
    assert reference_bar is not None
    assert reference_bar.close is not None
    assert horizon_bar is not None
    assert horizon_bar.close is not None
    change_pct = (horizon_bar.close / reference_bar.close - 1.0) * 100.0
    payload["post_expiry"] = {
        "horizon_sessions": _HORIZON_SESSIONS,
        "reference_date": reference_date.isoformat(),
        "reference_raw_close_yen": reference_bar.close,
        "horizon_date": horizon_date.isoformat(),
        "horizon_raw_close_yen": horizon_bar.close,
        "observed_price_change_pct": round(change_pct, 6),
    }
    return payload


def _result(
    target: _ObservationTarget,
    status: str,
    reason: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "reservation_id": target.reservation.reservation_id,
        "ticker": target.reservation.ticker,
        "remaining_quantity": target.remaining_quantity,
        "limit_yen": str(target.reservation.price_guard_yen),
        "asof": target.asof.isoformat(),
    }
    if reason is not None:
        payload["reason"] = reason
    if details is not None:
        payload["details"] = details
    return payload


def _unresolved_for_dates(
    target: _ObservationTarget, reason: str, dates: list[date]
) -> dict[str, object]:
    return _result(
        target,
        "unresolved",
        reason,
        {"dates": [day.isoformat() for day in dates]},
    )


def _reservation(document: PortfolioLedgerDocument, reservation_id: str) -> ReservationEvent:
    matches = [
        event
        for event in document.events
        if isinstance(event, ReservationEvent) and event.reservation_id == reservation_id
    ]
    if len(matches) != 1:
        raise _LimitOutcomeError(f"reservation_id must identify one reservation: {reservation_id}")
    return matches[0]


def _release(document: PortfolioLedgerDocument, reservation_id: str) -> ReleaseEvent | None:
    matches = [
        event
        for event in document.events
        if isinstance(event, ReleaseEvent) and event.reservation_id == reservation_id
    ]
    if len(matches) > 1:
        raise _LimitOutcomeError(f"reservation has multiple release events: {reservation_id}")
    return matches[0] if matches else None


def _expiry_covers_full_session(reservation: ReservationEvent) -> bool:
    local_expiry = reservation.expires_at.astimezone(_JST)
    return local_expiry.timetz().replace(tzinfo=None) >= _JPX_SESSION_CLOSE


def _is_nonpositive(value: float | None) -> bool:
    return value is not None and value <= 0


if __name__ == "__main__":
    raise SystemExit(main())
