"""Read-only original-valuation price watch for tickers with promoted research theses."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import cast

import yaml

from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
)
from baibai_engine.market.sqlite import (
    SQLiteSchemaError,
    optional_float,
    range_covered,
    validate_current_schema,
)
from baibai_engine.position.ledger import (
    PortfolioLedgerError,
    ReservationEvent,
    replay_events_through,
    reservation_snapshots,
)
from baibai_engine.position.store import (
    LedgerConflictError,
    LedgerSchemaError,
    load_ledger_in_transaction,
)
from baibai_engine.read_api import (
    list_thesis_publications,
)
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisError,
    ThesisReview,
)

_GAP_QUANTUM = Decimal("0.000001")
_ADJUSTMENT_FACTOR_ABS_TOLERANCE = 1e-12
_MIN_UTC = datetime.min.replace(tzinfo=UTC)


class ResearchPriceWatchError(ValueError):
    """Raised when the watch cannot be produced without inventing facts."""


@dataclass(frozen=True, slots=True)
class _ThesisPublication:
    thesis_id: str | Path
    document: ThesisDocument
    # The identity the thesis was published with. This tool only reads the store, so
    # every publication has one and the field carries no draft default.
    core_sha256: str
    published_at: datetime = _MIN_UTC
    review: ThesisReview | None = None


@dataclass(frozen=True, slots=True)
class _RawBar:
    traded_at: date
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
        prog="python -m baibai_engine.research_watch",
        description=(
            "Emit a read-only YAML watch for the latest promoted research thesis of every "
            "ticker, with candidate, defer, or reject disposition. "
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
        # A published thesis without a recorded identity refuses through this one.
        ThesisError,
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
    latest = _load_latest_promoted_theses(app_db_path, asof=asof)
    watched = dict(latest)

    with closing(connect_read_only(app_db_path)) as connection:
        connection.execute("BEGIN")
        ledger, _ = load_ledger_in_transaction(connection)
    state = replay_events_through(ledger.events, ledger.as_of)
    held_tickers = {
        ticker for ticker, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
    }
    reserved_tickers = {item.ticker for item in reservation_snapshots(state)}
    reservation_history = _reservation_history(ledger.events)

    observations = _read_market_observations(
        sqlite_path,
        theses=watched,
        asof=asof,
    )
    rows = [
        _watch_row(
            ticker=ticker,
            thesis=publication.document,
            observation=observations[ticker],
            held=ticker in held_tickers,
            reserved=ticker in reserved_tickers,
            history=reservation_history.get(ticker, []),
        )
        for ticker, publication in watched.items()
    ]
    resolved = sorted(
        (row for row in rows if row["status"] == "resolved"),
        key=_resolved_sort_key,
    )
    unresolved = sorted(
        (row for row in rows if row["status"] == "unresolved"),
        key=lambda row: str(row["ticker"]),
    )
    raw_latest: dict[str, dict[str, object]] = {}
    for publication in list_thesis_publications(app_db_path):
        raw_latest.setdefault(str(publication["ticker"]), publication)
    for ticker, publication in raw_latest.items():
        if ticker not in latest:
            unresolved.append(
                {
                    "ticker": ticker,
                    "status": "unresolved",
                    "unresolved_reason": "requires_reassessment",
                    "thesis_id": publication["thesis_id"],
                    "thesis_as_of": publication["as_of"],
                    "current_decision_status": "not_evaluated",
                }
            )
    all_thesis_tickers = set(raw_latest)
    reservation_tickers = set(reservation_history)
    return {
        "market_asof": asof.isoformat(),
        "ledger_as_of": ledger.as_of.isoformat(),
        "rows": resolved + unresolved,
        "coverage": {
            "thesis_tickers": sorted(all_thesis_tickers),
            "watched_tickers": sorted(watched),
            "reservation_history_tickers": sorted(reservation_tickers),
            "ledger_only_tickers": sorted(reservation_tickers - all_thesis_tickers),
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


def _load_latest_promoted_theses(app_db_path: Path, *, asof: date) -> dict[str, _ThesisPublication]:
    from contextlib import closing

    from baibai_engine.appdb.read import connect_read_only
    from baibai_engine.research.thesis_store import (
        ResearchValidationError,
        load_latest_reviewed_thesis,
    )

    latest: dict[str, _ThesisPublication] = {}
    with closing(connect_read_only(app_db_path)) as connection:
        for row in connection.execute("SELECT DISTINCT ticker FROM thesis").fetchall():
            ticker = str(row[0])
            try:
                pair = load_latest_reviewed_thesis(connection, ticker)
            except ResearchValidationError:
                continue
            if pair.document.input_snapshot.as_of > asof:
                raise ResearchPriceWatchError("future thesis cannot be used in watch")
            latest[ticker] = _ThesisPublication(
                thesis_id=pair.thesis_id,
                document=pair.document,
                review=pair.review,
                core_sha256=pair.core_sha256,
            )
    return latest


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
                "occurred_at": event.occurred_at.isoformat(),
                "quantity": event.quantity,
                "price_guard_yen": _decimal_number(event.price_guard_yen),
                "expires_at": event.expires_at.isoformat(),
            }
        )
    return history


def _read_market_observations(
    sqlite_path: Path,
    *,
    theses: dict[str, _ThesisPublication],
    asof: date,
) -> dict[str, _MarketObservation]:
    if not sqlite_path.is_file():
        raise ResearchPriceWatchError(f"market SQLite does not exist: {sqlite_path}")
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        validate_current_schema(conn)
        start = min(
            (_price_basis_date(publication.document) for publication in theses.values()),
            default=asof,
        )
        if not range_covered(conn, "jquants_market_calendar", start, asof):
            raise ResearchPriceWatchError(
                f"market calendar does not cover {start.isoformat()}..{asof.isoformat()}"
            )
        calendar_rows = conn.execute(
            "SELECT day, is_business_day FROM jquants_market_calendar "
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
        if asof not in calendar:
            raise ResearchPriceWatchError(
                f"market calendar has no exact row for asof {asof.isoformat()}"
            )
        if not calendar[asof]:
            raise ResearchPriceWatchError(f"asof is not a business day: {asof.isoformat()}")
        bars_by_ticker: dict[str, dict[date, _RawBar]] = defaultdict(dict)
        if theses:
            placeholders = ",".join("?" for _ in theses)
            # The f-string only expands "?" placeholders; every value is parameter-bound.
            bar_rows = conn.execute(
                "SELECT ticker, traded_at, close, adjustment_factor "
                "FROM jquants_daily_bars "
                f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "  # nosec B608
                "ORDER BY ticker, traded_at",
                (*sorted(theses), start.isoformat(), asof.isoformat()),
            ).fetchall()
            for ticker, traded_at, close, factor in bar_rows:
                day = date.fromisoformat(str(traded_at))
                bars_by_ticker[str(ticker)][day] = _RawBar(
                    traded_at=day,
                    close=optional_float(close),
                    adjustment_factor=optional_float(factor),
                )
        return {
            ticker: _observe_ticker(
                thesis=publication.document,
                asof=asof,
                calendar=calendar,
                bars=bars_by_ticker[ticker],
            )
            for ticker, publication in theses.items()
        }
    except (TypeError, ValueError) as error:
        if isinstance(error, ResearchPriceWatchError):
            raise
        raise ResearchPriceWatchError(f"corrupt market snapshot: {error}") from error
    finally:
        conn.rollback()
        conn.close()


def _price_basis_date(thesis: ThesisDocument) -> date:
    return next(
        (
            fact.as_of
            for fact in thesis.input_snapshot.facts
            if fact.fact_id == thesis.valuation.market_price_fact_id
        ),
        thesis.input_snapshot.as_of,
    )


def _observe_ticker(
    *,
    thesis: ThesisDocument,
    asof: date,
    calendar: dict[date, bool],
    bars: dict[date, _RawBar],
) -> _MarketObservation:
    expected_sessions = [
        day
        for day, is_business_day in calendar.items()
        if is_business_day and _price_basis_date(thesis) <= day <= asof
    ]
    current_bar = bars.get(asof)
    current_close = _valid_close(current_bar.close) if current_bar is not None else None
    close_as_of = asof if current_close is not None else None
    for session in expected_sessions:
        if session not in bars:
            return _MarketObservation(current_close, close_as_of, f"missing_raw_bar:{session}")
    for session in expected_sessions:
        bar = bars[session]
        if bar.close is None:
            return _MarketObservation(current_close, close_as_of, f"missing_raw_close:{session}")
        if _valid_close(bar.close) is None:
            return _MarketObservation(current_close, close_as_of, f"invalid_raw_close:{session}")
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
    thesis: ThesisDocument,
    observation: _MarketObservation,
    held: bool,
    reserved: bool,
    history: list[dict[str, object]],
) -> dict[str, object]:
    from baibai_engine.research.valuation import maximum_entry_price

    valuation = thesis.valuation
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
        if maximum is None or observation.current_close_yen is None
        else _calculate_gap(maximum, observation.current_close_yen)
    )
    return {
        "ticker": ticker,
        "status": "unresolved" if unresolved else "resolved",
        "current_close_yen": observation.current_close_yen,
        "close_as_of": None
        if observation.close_as_of is None
        else observation.close_as_of.isoformat(),
        "pmax_raw_yen": None if maximum is None else _decimal_number(maximum),
        "pmax_gap_pct": gap,
        "thesis_disposition": thesis.judgment.disposition,
        "thesis_as_of": thesis.input_snapshot.as_of.isoformat(),
        "horizon_months": valuation.horizon_months,
        "original_quote_as_of": _price_basis_date(thesis).isoformat(),
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


def _decimal_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _float_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    if value.is_integer():
        return int(value)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
