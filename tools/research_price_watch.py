"""Read-only re-entry watch for tickers with promoted research packets."""

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
    load_portfolio_ledger,
    replay_events_through,
    reservation_snapshots,
)
from baibai_engine.read_api import (
    list_research_packet_publications,
    list_research_review_publications,
)
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    IndependentReview,
    evaluate_decision_packet,
)

_GAP_QUANTUM = Decimal("0.000001")
_ADJUSTMENT_FACTOR_ABS_TOLERANCE = 1e-12
class ResearchPriceWatchError(ValueError):
    """Raised when the watch cannot be produced without inventing facts."""


@dataclass(frozen=True, slots=True)
class _PacketCandidate:
    packet_id: str | Path
    document: DecisionPacketDocument
    published_at: datetime = datetime.min.replace(tzinfo=UTC)
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
            "Emit a read-only YAML watch for latest promoted buy/defer research packets. "
            "This command never proposes an order or updates canonical records."
        ),
    )
    parser.add_argument("--db", type=Path, default=Path("data/app/baibai.sqlite"))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--asof", type=_date_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_watch(
            app_db_path=args.db,
            ledger_path=args.ledger,
            sqlite_path=args.sqlite_path,
            asof=args.asof,
        )
    except (
        PortfolioLedgerError,
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
    ledger_path: Path,
    sqlite_path: Path,
    asof: date,
) -> dict[str, object]:
    latest = _load_latest_promoted_packets(app_db_path, asof=asof)
    watched = {
        ticker: candidate
        for ticker, candidate in latest.items()
        if candidate.document.judgment.recommendation in {"buy", "defer"}
    }
    excluded_reject = sorted(
        ticker
        for ticker, candidate in latest.items()
        if candidate.document.judgment.recommendation == "reject"
    )

    ledger = load_portfolio_ledger(ledger_path)
    state = replay_events_through(ledger.events, ledger.as_of)
    held_tickers = {
        ticker for ticker, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
    }
    reserved_tickers = {item.ticker for item in reservation_snapshots(state)}
    reservation_history = _reservation_history(ledger.events)

    observations = _read_market_observations(
        sqlite_path,
        packets=watched,
        asof=asof,
    )
    rows = [
        _watch_row(
            ticker=ticker,
            packet=candidate.document,
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
    all_packet_tickers = set(latest)
    reservation_tickers = set(reservation_history)
    return {
        "market_asof": asof.isoformat(),
        "ledger_as_of": ledger.as_of.isoformat(),
        "rows": resolved + unresolved,
        "coverage": {
            "packet_tickers": sorted(all_packet_tickers),
            "watched_tickers": sorted(watched),
            "reservation_history_tickers": sorted(reservation_tickers),
            "ledger_only_tickers": sorted(reservation_tickers - all_packet_tickers),
            "excluded_latest_reject_tickers": excluded_reject,
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


def _load_latest_promoted_packets(
    app_db_path: Path,
    *,
    asof: date,
) -> dict[str, _PacketCandidate]:
    if not app_db_path.is_file():
        raise ResearchPriceWatchError(f"application database does not exist: {app_db_path}")
    reviews_by_packet: dict[str, list[dict[str, object]]] = defaultdict(list)
    for publication in list_research_review_publications(app_db_path):
        reviews_by_packet[str(publication["packet_id"])].append(publication)
    candidates: list[_PacketCandidate] = []
    for publication in list_research_packet_publications(app_db_path):
        packet_id = str(publication["packet_id"])
        packet_payload = publication["payload"]
        if not isinstance(packet_payload, dict):
            raise ResearchPriceWatchError(f"packet payload is invalid: {packet_id}")
        document = DecisionPacketDocument.model_validate(packet_payload)
        if document.input_snapshot.as_of > asof:
            raise ResearchPriceWatchError(
                f"future decision packet is not allowed: {packet_id} "
                f"({document.input_snapshot.as_of.isoformat()} > {asof.isoformat()})"
            )
        review_publications = reviews_by_packet.get(packet_id, [])
        if not review_publications:
            raise ResearchPriceWatchError(
                f"promoted decision packet requires an independent review: {packet_id}"
            )
        review_payload = review_publications[0]["payload"]
        if not isinstance(review_payload, dict):
            raise ResearchPriceWatchError(f"review payload is invalid: {packet_id}")
        candidates.append(
            _PacketCandidate(
                packet_id=packet_id,
                published_at=datetime.fromisoformat(str(publication["published_at"])),
                document=document,
                review=IndependentReview.model_validate(review_payload),
            )
        )
    latest = _select_latest_packets(candidates)
    for ticker, candidate in latest.items():
        if candidate.review is None:  # pragma: no cover - loader invariant
            raise ResearchPriceWatchError(
                f"promoted decision packet requires an independent review: {candidate.packet_id}"
            )
        result = evaluate_decision_packet(
            candidate.document,
            review=candidate.review,
            now=_historical_integrity_evaluated_at(candidate.document, candidate.review),
        )
        if result.errors or result.decision_readiness != "ready":
            details = "; ".join(result.errors) or result.packet_status
            raise ResearchPriceWatchError(
                f"latest decision packet for {ticker} is not ready: {details}"
            )
    return latest


def _historical_integrity_evaluated_at(
    document: DecisionPacketDocument,
    review: IndependentReview,
) -> datetime:
    """Rebuild artifact integrity without treating an expired override as a current signal."""
    anchors = [document.judgment.proposed_at, review.reviewed_at]
    if document.human_evidence_override is not None:
        anchors.append(document.human_evidence_override.approved_at)
    return max(anchors)


def _select_latest_packets(
    candidates: list[_PacketCandidate],
) -> dict[str, _PacketCandidate]:
    by_ticker: dict[str, list[_PacketCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_ticker[candidate.document.input_snapshot.ticker].append(candidate)
    selected: dict[str, _PacketCandidate] = {}
    for ticker, ticker_candidates in by_ticker.items():
        selected[ticker] = max(
            ticker_candidates,
            key=lambda item: (
                item.document.input_snapshot.as_of,
                item.published_at,
                item.packet_id,
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
    packets: dict[str, _PacketCandidate],
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
            (candidate.document.input_snapshot.as_of for candidate in packets.values()),
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
        if packets:
            placeholders = ",".join("?" for _ in packets)
            bar_rows = conn.execute(
                "SELECT ticker, traded_at, close, adjustment_factor "
                "FROM jquants_daily_bars "
                f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "
                "ORDER BY ticker, traded_at",
                (*sorted(packets), start.isoformat(), asof.isoformat()),
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
                packet=candidate.document,
                asof=asof,
                calendar=calendar,
                bars=bars_by_ticker[ticker],
            )
            for ticker, candidate in packets.items()
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
    packet: DecisionPacketDocument,
    asof: date,
    calendar: dict[date, bool],
    bars: dict[date, _RawBar],
) -> _MarketObservation:
    expected_sessions = [
        day
        for day, is_business_day in calendar.items()
        if is_business_day and packet.input_snapshot.as_of <= day <= asof
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
    packet: DecisionPacketDocument,
    observation: _MarketObservation,
    held: bool,
    reserved: bool,
    history: list[dict[str, object]],
) -> dict[str, object]:
    unresolved = observation.unresolved_reason
    fair_value = packet.estimates.current_fair_value_yen
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
        "packet_fair_value_yen": _decimal_number(fair_value),
        "packet_fv_gap_pct": gap,
        "packet_entry_price_basis_yen": _decimal_number(packet.estimates.entry_price_basis_yen),
        "packet_recommendation_at_as_of": packet.judgment.recommendation,
        "packet_as_of": packet.input_snapshot.as_of.isoformat(),
        "valuation_model_version": packet.estimates.valuation_model_version,
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
    return (-cast(float, row["packet_fv_gap_pct"]), str(row["ticker"]))


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
