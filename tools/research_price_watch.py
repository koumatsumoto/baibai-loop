"""Read-only re-entry watch for tickers with promoted research theses."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import cast

import yaml

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
from baibai_engine.position.store import LedgerConflictError, LedgerSchemaError, LedgerStoreService
from baibai_engine.read_api import (
    list_thesis_publications,
    list_thesis_review_publications,
)
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    evaluate_thesis,
)

_GAP_QUANTUM = Decimal("0.000001")
_ADJUSTMENT_FACTOR_ABS_TOLERANCE = 1e-12
_MIN_UTC = datetime.min.replace(tzinfo=UTC)


class ResearchPriceWatchError(ValueError):
    """Raised when the watch cannot be produced without inventing facts."""


@dataclass(frozen=True, slots=True)
class _ThesisCandidate:
    thesis_id: str | Path
    document: ThesisDocument
    published_at: datetime = _MIN_UTC
    review: IndependentReview | None = None


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
        prog="python -m tools.research_price_watch",
        description=(
            "Emit a read-only YAML watch for latest promoted buy/defer research theses. "
            "This command never proposes an order or updates canonical records."
        ),
    )
    parser.add_argument("--db", type=Path, default=Path("data/app/baibai.sqlite"))
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--asof", type=_date_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    # reject も watch する。深掘りの結論は「この価格では買わない」であって「二度と見ない」
    # ではなく、bargain assessment は研究 FV を再評価条件として名指ししている。除外すると
    # 一次情報まで降りて出した FV が、価格が降りてきたときに誰も読まない値になる。
    watched = dict(latest)

    ledger = LedgerStoreService(app_db_path).load()
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
            thesis=candidate.document,
            observation=observations[ticker],
            held=ticker in held_tickers,
            reserved=ticker in reserved_tickers,
            history=reservation_history.get(ticker, []),
        )
        for ticker, candidate in watched.items()
    ]
    resolved = sorted(
        (row for row in rows if row["status"] == "resolved"),
        key=_resolved_sort_key,
    )
    unresolved = sorted(
        (row for row in rows if row["status"] == "unresolved"),
        key=lambda row: str(row["ticker"]),
    )
    all_thesis_tickers = set(latest)
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
        "triggered": [
            {
                "ticker": row["ticker"],
                "current_close_yen": row["current_close_yen"],
                "thesis_fair_value_yen": row["thesis_fair_value_yen"],
                "thesis_recommendation_at_as_of": row["thesis_recommendation_at_as_of"],
                "current_portfolio_status": row["current_portfolio_status"],
            }
            for row in resolved
            if row["fair_value_reached"]
        ],
        "diagnostics": {
            "re_research_required_for_all_rows": True,
            "decision_status": "not_evaluated",
            "market_price_basis": "jquants_raw_close",
            "portfolio_status_basis": "canonical_ledger_as_of",
        },
    }


def _load_latest_promoted_theses(
    app_db_path: Path,
    *,
    asof: date,
) -> dict[str, _ThesisCandidate]:
    if not app_db_path.is_file():
        raise ResearchPriceWatchError(f"application database does not exist: {app_db_path}")
    reviews_by_thesis: dict[str, list[dict[str, object]]] = defaultdict(list)
    for publication in list_thesis_review_publications(app_db_path):
        reviews_by_thesis[str(publication["thesis_id"])].append(publication)
    candidates: list[_ThesisCandidate] = []
    for publication in list_thesis_publications(app_db_path):
        thesis_id = str(publication["thesis_id"])
        thesis_payload = publication["payload"]
        if not isinstance(thesis_payload, dict):
            raise ResearchPriceWatchError(f"thesis payload is invalid: {thesis_id}")
        document = ThesisDocument.model_validate(thesis_payload)
        if document.input_snapshot.as_of > asof:
            raise ResearchPriceWatchError(
                f"future thesis is not allowed: {thesis_id} "
                f"({document.input_snapshot.as_of.isoformat()} > {asof.isoformat()})"
            )
        review_publications = reviews_by_thesis.get(thesis_id, [])
        if not review_publications:
            raise ResearchPriceWatchError(
                f"promoted thesis requires an independent review: {thesis_id}"
            )
        review_payload = review_publications[0]["payload"]
        if not isinstance(review_payload, dict):
            raise ResearchPriceWatchError(f"review payload is invalid: {thesis_id}")
        candidates.append(
            _ThesisCandidate(
                thesis_id=thesis_id,
                published_at=datetime.fromisoformat(str(publication["published_at"])),
                document=document,
                review=IndependentReview.model_validate(review_payload),
            )
        )
    latest = _select_latest_theses(candidates)
    for ticker, candidate in latest.items():
        if candidate.review is None:  # pragma: no cover - loader invariant
            raise ResearchPriceWatchError(
                f"promoted thesis requires an independent review: {candidate.thesis_id}"
            )
        result = evaluate_thesis(
            candidate.document,
            review=candidate.review,
            now=_historical_integrity_evaluated_at(candidate.document, candidate.review),
        )
        if result.errors or result.decision_readiness != "ready":
            details = "; ".join(result.errors) or result.thesis_status
            raise ResearchPriceWatchError(f"latest thesis for {ticker} is not ready: {details}")
    return latest


def _historical_integrity_evaluated_at(
    document: ThesisDocument,
    review: IndependentReview,
) -> datetime:
    """Rebuild artifact integrity without treating an expired override as a current signal."""
    anchors = [document.judgment.proposed_at, review.reviewed_at]
    if document.human_evidence_override is not None:
        anchors.append(document.human_evidence_override.approved_at)
    return max(anchors)


def _select_latest_theses(
    candidates: list[_ThesisCandidate],
) -> dict[str, _ThesisCandidate]:
    by_ticker: dict[str, list[_ThesisCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_ticker[candidate.document.input_snapshot.ticker].append(candidate)
    selected: dict[str, _ThesisCandidate] = {}
    for ticker, ticker_candidates in by_ticker.items():
        selected[ticker] = max(
            ticker_candidates,
            key=lambda item: (
                item.document.input_snapshot.as_of,
                item.published_at,
                item.thesis_id,
            ),
        )
    return selected


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
    theses: dict[str, _ThesisCandidate],
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
            (candidate.document.input_snapshot.as_of for candidate in theses.values()),
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
                thesis=candidate.document,
                asof=asof,
                calendar=calendar,
                bars=bars_by_ticker[ticker],
            )
            for ticker, candidate in theses.items()
        }
    except (TypeError, ValueError) as error:
        if isinstance(error, ResearchPriceWatchError):
            raise
        raise ResearchPriceWatchError(f"corrupt market snapshot: {error}") from error
    finally:
        conn.rollback()
        conn.close()


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
        if is_business_day and thesis.input_snapshot.as_of <= day <= asof
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
    unresolved = observation.unresolved_reason
    fair_value = thesis.estimates.current_fair_value_yen
    gap: float | None = None
    if unresolved is None and observation.current_close_yen is not None:
        gap = _calculate_gap(fair_value, observation.current_close_yen)
        if gap is None:
            unresolved = "fv_gap_calculation_unresolved"
    return {
        "ticker": ticker,
        "status": "unresolved" if unresolved is not None else "resolved",
        "current_close_yen": _float_number(observation.current_close_yen),
        "close_as_of": observation.close_as_of.isoformat() if observation.close_as_of else None,
        "thesis_fair_value_yen": _decimal_number(fair_value),
        "thesis_fv_gap_pct": gap,
        # 終値が研究 FV 以下へ降りてきたか。買い注文ではなく「読み直す理由が発生した」の
        # 合図であり、指値も数量もここでは決めない。
        "fair_value_reached": unresolved is None and gap is not None and gap >= 0,
        "thesis_entry_price_basis_yen": _decimal_number(thesis.estimates.entry_price_basis_yen),
        "thesis_recommendation_at_as_of": thesis.judgment.recommendation,
        "thesis_as_of": thesis.input_snapshot.as_of.isoformat(),
        "valuation_model_version": thesis.estimates.valuation_model_version,
        "re_research_required": True,
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
    return (-cast(float, row["thesis_fv_gap_pct"]), str(row["ticker"]))


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
