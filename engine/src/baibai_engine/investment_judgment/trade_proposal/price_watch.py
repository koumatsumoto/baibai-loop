"""Read-only original-valuation price watch for tickers with published Stock Assessments."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import cast

import yaml

from baibai_engine.primitives.numbers import decimal_to_number
from baibai_engine.storage.paths import (
    APPLICATION_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
)
from baibai_engine.investment_judgment.contracts import (
    AssessmentError,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
)
from baibai_engine.investment_judgment.stock_assessment.store import ResearchValidationError
from baibai_engine.portfolio.ledger.events import (
    PortfolioLedgerError,
    ReservationEvent,
    replay_events_through,
    reservation_snapshots,
)
from baibai_engine.portfolio.ledger.read import (
    LedgerConflictError,
    LedgerSchemaError,
    load_ledger_in_transaction,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.convert import optional_float
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, validate_current_schema

_GAP_QUANTUM = Decimal("0.000001")
_ADJUSTMENT_FACTOR_ABS_TOLERANCE = 1e-12


class ResearchPriceWatchError(ValueError):
    """Raised when the watch cannot be produced without inventing facts."""


@dataclass(frozen=True, slots=True)
class _AssessmentPublication:
    assessment_id: str
    document: StockAssessmentDocument


@dataclass(frozen=True, slots=True)
class _RawBar:
    close: float | None
    adjustment_factor: float | None


@dataclass(frozen=True, slots=True)
class _MarketObservation:
    current_close_yen: float | None
    close_as_of: date | None
    unresolved_reason: str | None


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baibai_engine.investment_judgment.trade_proposal.price_watch",
        description=(
            "Emit a read-only YAML watch for the latest published Stock Assessment of every "
            "ticker, with supported, uncertain, or unsupported stance. "
            "This command never "
            "proposes an order or updates canonical records."
        ),
    )
    parser.add_argument("--db", type=Path, default=APPLICATION_DB_PATH)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--asof", type=_date_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reject_noncanonical_store_paths()
    except StoreLayoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        payload = build_watch(
            app_db_path=args.db,
            sqlite_path=args.sqlite_path,
            asof=args.asof,
        )
    except (
        PortfolioLedgerError,
        LedgerConflictError,
        LedgerSchemaError,
        ResearchPriceWatchError,
        SQLiteSchemaError,
        # A published assessment without a recorded identity refuses through this one.
        AssessmentError,
        ResearchValidationError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def build_watch(
    *,
    app_db_path: Path,
    sqlite_path: Path,
    asof: date,
) -> dict[str, object]:
    with closing(connect_read_only(app_db_path)) as connection:
        connection.execute("BEGIN")
        latest, raw_latest = _load_latest_assessments(connection, asof=asof)
        ledger, _ = load_ledger_in_transaction(connection)
    state = replay_events_through(ledger.events, ledger.as_of)
    held_tickers = {
        ticker for ticker, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
    }
    reserved_tickers = {item.ticker for item in reservation_snapshots(state)}
    reservation_history = _reservation_history(ledger.events)

    observations = _read_market_observations(
        sqlite_path,
        assessments=latest,
        asof=asof,
    )
    rows = [
        _watch_row(
            ticker=ticker,
            assessment_id=publication.assessment_id,
            assessment=publication.document,
            observation=observations[ticker],
            held=ticker in held_tickers,
            reserved=ticker in reserved_tickers,
            history=reservation_history.get(ticker, []),
        )
        for ticker, publication in latest.items()
    ]
    resolved = sorted(
        (row for row in rows if row["status"] == "resolved"),
        key=_resolved_sort_key,
    )
    unresolved = sorted(
        (row for row in rows if row["status"] == "unresolved"),
        key=lambda row: str(row["ticker"]),
    )
    for ticker, publication in raw_latest.items():
        if ticker not in latest:
            unresolved.append(
                {
                    "ticker": ticker,
                    "status": "unresolved",
                    "unresolved_reason": "requires_reassessment",
                    "assessment_id": publication["assessment_id"],
                    "assessment_as_of": publication["as_of"],
                    "current_decision_status": "not_evaluated",
                }
            )
    all_assessment_tickers = set(raw_latest)
    reservation_tickers = set(reservation_history)
    return {
        "market_asof": asof.isoformat(),
        "ledger_as_of": ledger.as_of.isoformat(),
        "rows": resolved + unresolved,
        "coverage": {
            "assessment_tickers": sorted(all_assessment_tickers),
            "watched_tickers": sorted(latest),
            "reservation_history_tickers": sorted(reservation_tickers),
            "ledger_only_tickers": sorted(reservation_tickers - all_assessment_tickers),
            "resolved_count": len(resolved),
            "unresolved_count": len(unresolved),
        },
        "diagnostics": {
            "re_research_required_for_all_rows": True,
            "decision_status": "not_evaluated",
            "market_price_basis": "jquants_raw_close",
            "portfolio_status_basis": "canonical_ledger_as_of",
        },
    }


def _load_latest_assessments(
    connection: sqlite3.Connection, *, asof: date
) -> tuple[dict[str, _AssessmentPublication], dict[str, dict[str, object]]]:
    from baibai_engine.investment_judgment.stock_assessment.store import (
        latest_assessment_id,
        load_reviewed_assessment,
    )

    latest: dict[str, _AssessmentPublication] = {}
    headers: dict[str, dict[str, object]] = {}
    for row in connection.execute("SELECT DISTINCT ticker FROM stock_assessment").fetchall():
        ticker = str(row[0])
        identity = latest_assessment_id(connection, ticker)
        assert identity is not None
        header = connection.execute(
            "SELECT as_of FROM stock_assessment WHERE assessment_id=?", (identity,)
        ).fetchone()
        if date.fromisoformat(str(header[0])) > asof:
            raise ResearchPriceWatchError("future assessment cannot be used in watch")
        headers[ticker] = {"assessment_id": identity, "as_of": str(header[0])}
        try:
            pair = load_reviewed_assessment(connection, identity)
        except ResearchValidationError:
            continue
        latest[ticker] = _AssessmentPublication(
            assessment_id=pair.assessment_id,
            document=pair.document,
        )
    return latest, headers


def _reservation_history(
    events: tuple[object, ...],
) -> dict[str, list[dict[str, object]]]:
    history: dict[str, list[dict[str, object]]] = defaultdict(list)
    reservations = sorted(
        (event for event in events if isinstance(event, ReservationEvent)),
        key=lambda event: (event.occurred_at, event.reservation_id),
    )
    for event in reservations:
        history[event.ticker].append(
            {
                "reservation_id": event.reservation_id,
                "occurred_on": None if event.occurred_on is None else event.occurred_on.isoformat(),
                "occurred_at": event.occurred_at.isoformat() if event.occurred_on is None else None,
                "quantity": event.quantity,
                "price_guard_yen": decimal_to_number(event.price_guard_yen),
                "expires_at": event.expires_at.isoformat()
                if event.expires_at is not None
                else None,
            }
        )
    return history


def _read_market_observations(
    sqlite_path: Path,
    *,
    assessments: dict[str, _AssessmentPublication],
    asof: date,
) -> dict[str, _MarketObservation]:
    if not assessments:
        return {}
    if not sqlite_path.is_file():
        raise ResearchPriceWatchError(f"market SQLite does not exist: {sqlite_path}")
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        validate_current_schema(conn)
        start = min(
            (
                min(asof, _price_basis_date(publication.document) + timedelta(days=1))
                for publication in assessments.values()
            ),
            default=asof,
        )
        if not range_covered(conn, "jquants_market_calendar", start, asof):
            raise ResearchPriceWatchError(
                f"market calendar does not cover {start.isoformat()}..{asof.isoformat()}"
            )
        calendar_rows = conn.execute(
            "SELECT day, holiday_division IN ('1', '2') FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), asof.isoformat()),
        ).fetchall()
        calendar = {
            date.fromisoformat(str(day)): bool(is_business_day)
            for day, is_business_day in calendar_rows
        }
        expected_calendar_days = {
            start + timedelta(days=offset) for offset in range((asof - start).days + 1)
        }
        missing_calendar_days = sorted(expected_calendar_days - set(calendar))
        if missing_calendar_days:
            raise ResearchPriceWatchError(
                "market calendar has missing row: "
                + ", ".join(day.isoformat() for day in missing_calendar_days)
            )
        if not calendar[asof]:
            raise ResearchPriceWatchError(f"asof is not a business day: {asof.isoformat()}")
        bars_by_ticker: dict[str, dict[date, _RawBar]] = defaultdict(dict)
        if assessments:
            placeholders = ",".join("?" for _ in assessments)
            # The f-string only expands "?" placeholders; every value is parameter-bound.
            bar_rows = conn.execute(
                "SELECT ticker, traded_at, close, adjustment_factor "
                "FROM jquants_daily_bars "
                f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "  # nosec B608
                "ORDER BY ticker, traded_at",
                (*sorted(assessments), start.isoformat(), asof.isoformat()),
            ).fetchall()
            for ticker, traded_at, close, factor in bar_rows:
                day = date.fromisoformat(str(traded_at))
                bars_by_ticker[str(ticker)][day] = _RawBar(
                    close=optional_float(close),
                    adjustment_factor=optional_float(factor),
                )
        return {
            ticker: _observe_ticker(
                assessment=publication.document,
                asof=asof,
                calendar=calendar,
                bars=bars_by_ticker[ticker],
            )
            for ticker, publication in assessments.items()
        }
    except (TypeError, ValueError) as error:
        if isinstance(error, ResearchPriceWatchError):
            raise
        raise ResearchPriceWatchError(f"corrupt market snapshot: {error}") from error
    finally:
        conn.rollback()
        conn.close()


def _price_basis_date(assessment: StockAssessmentDocument) -> date:
    return next(
        (
            fact.as_of
            for fact in assessment.input_snapshot.facts
            if fact.fact_id == assessment.valuation.market_price_fact_id
        ),
        assessment.input_snapshot.as_of,
    )


def _observe_ticker(
    *,
    assessment: StockAssessmentDocument,
    asof: date,
    calendar: dict[date, bool],
    bars: dict[date, _RawBar],
) -> _MarketObservation:
    basis_date = _price_basis_date(assessment)
    current_bar = bars.get(asof)
    if current_bar is None:
        return _MarketObservation(None, None, f"missing_raw_bar:{asof}")
    current_close = _valid_close(current_bar.close)
    if current_close is None:
        reason = "missing_raw_close" if current_bar.close is None else "invalid_raw_close"
        return _MarketObservation(None, None, f"{reason}:{asof}")
    close_as_of = asof
    # The original quote already uses its own day's basis. Only later sessions
    # can change that basis; intermediate close values are not inputs to this gap.
    for session, is_business_day in calendar.items():
        if not is_business_day or not basis_date < session <= asof:
            continue
        bar = bars.get(session)
        if bar is None:
            return _MarketObservation(current_close, close_as_of, f"missing_raw_bar:{session}")
        if bar.adjustment_factor is None:
            return _MarketObservation(
                current_close,
                close_as_of,
                f"corporate_action_basis_unassessed:{session}",
            )
        if not math.isfinite(bar.adjustment_factor) or not math.isclose(
            bar.adjustment_factor,
            1.0,
            rel_tol=0.0,
            abs_tol=_ADJUSTMENT_FACTOR_ABS_TOLERANCE,
        ):
            return _MarketObservation(
                current_close,
                close_as_of,
                f"corporate_action_in_comparison_window:{session}",
            )
    return _MarketObservation(current_close, close_as_of, None)


def _valid_close(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def _watch_row(
    *,
    ticker: str,
    assessment_id: str,
    assessment: StockAssessmentDocument,
    observation: _MarketObservation,
    held: bool,
    reserved: bool,
    history: list[dict[str, object]],
) -> dict[str, object]:
    from baibai_engine.investment_judgment.stock_assessment.scenario import maximum_entry_price

    valuation = assessment.valuation
    maximum = None
    if (
        valuation.base is not None
        and valuation.horizon_months is not None
        and valuation.required_annual_return_pct is not None
    ):
        maximum = maximum_entry_price(
            valuation.base,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
    unresolved = observation.unresolved_reason or (
        "valuation_unresolved" if maximum is None else None
    )
    gap = (
        None
        if unresolved is not None or maximum is None or observation.current_close_yen is None
        else _calculate_gap(maximum, observation.current_close_yen)
    )
    if unresolved is None and gap is None:
        unresolved = "price_comparison_unavailable"
    return {
        "ticker": ticker,
        "status": "unresolved" if unresolved else "resolved",
        "current_close_yen": observation.current_close_yen,
        "close_as_of": None
        if observation.close_as_of is None
        else observation.close_as_of.isoformat(),
        "pmax_raw_yen": None if maximum is None else decimal_to_number(maximum),
        "pmax_gap_pct": gap,
        "assessment_id": assessment_id,
        "assessment_stance": assessment.conclusion.stance,
        "assessment_as_of": assessment.input_snapshot.as_of.isoformat(),
        "horizon_months": valuation.horizon_months,
        "original_quote_as_of": _price_basis_date(assessment).isoformat(),
        "reference_basis": "原評価の累積分配・期間。現在の残存年率ではない",
        "current_decision_status": "not_evaluated",
        "current_portfolio_status": _portfolio_status(held=held, reserved=reserved),
        "reservation_history": history,
        "unresolved_reason": unresolved,
    }


def _calculate_gap(fair_value: Decimal, current_close_yen: float) -> float | None:
    try:
        calculated = (fair_value / Decimal(str(current_close_yen)) - Decimal(1)) * Decimal(100)
        gap = float(calculated.quantize(_GAP_QUANTUM, rounding=ROUND_HALF_UP))
    except (DecimalException, OverflowError, ValueError):
        return None
    return gap if math.isfinite(gap) else None


def _portfolio_status(*, held: bool, reserved: bool) -> str:
    if held and reserved:
        return "held_and_reserved"
    if held:
        return "held"
    if reserved:
        return "reserved"
    return "unheld"


def _resolved_sort_key(row: dict[str, object]) -> tuple[float, str]:
    return (-cast(float, row["pmax_gap_pct"]), str(row["ticker"]))


if __name__ == "__main__":
    raise SystemExit(main())
