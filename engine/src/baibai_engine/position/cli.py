"""Command-line entry for application-DB portfolio records.

Composes ledger, human-reported broker facts, and outcome inputs. This entry point owns
SQLite composition while position core modules remain free of screening
dependencies.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path

import yaml

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_engine.market.jpx_total_return import (
    BenchmarkObservationError,
    load_benchmark_observation,
)
from baibai_engine.market.store import read_portfolio_market_inputs
from baibai_engine.market_context.observations.benchmark import BenchmarkObservation
from baibai_engine.position.broker_fact_recording import BrokerFactRecordingError
from baibai_engine.position.decision_references import resolve_buy_reference
from baibai_engine.position.drafts import (
    HumanEvent,
    apply_draft,
    build_broker_fact_draft,
    build_event_draft,
    build_meta_draft,
    build_override_draft,
    build_sell_execution_draft,
    date_only_replay_key,
    known_broker_reference,
    load_draft,
    write_draft,
)
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    ExecutionEvent,
    HumanOverride,
    IncomeEvent,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    ReservationEvent,
    WithdrawalEvent,
    replay_events_through,
    require_resolved_expiries,
    snapshot_to_payload,
)
from baibai_engine.position.ledger_read import LedgerConflictError
from baibai_engine.position.market_source import market_basis_is_confirmed
from baibai_engine.position.outcome import compute_portfolio_outcome, outcome_to_payload
from baibai_engine.position.outcome_store import (
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.position.valuation import current_portfolio
from baibai_engine.storage.observations.read import connect_read_only


def _datetime_argument(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def _decimal_argument(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a decimal number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine position")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ledger_parser = subparsers.add_parser(
        "ledger",
        description="Reconcile the application-DB portfolio ledger and emit a YAML snapshot.",
        help="reconcile available cash, reservations, holdings, income, costs, and warnings",
    )
    ledger_parser.add_argument("--db", type=Path)
    outcome_parser = subparsers.add_parser(
        "outcome",
        help=(
            "report a portfolio outcome only from a canonical ledger and JPX "
            "gross-total-return observation"
        ),
    )
    outcome_parser.add_argument("--root", type=Path, default=Path.cwd())
    outcome_parser.add_argument("--db", type=Path)
    outcome_parser.add_argument("--benchmark-observation", type=Path, required=True)
    outcome_parser.add_argument(
        "--sqlite",
        type=Path,
        default=DEFAULT_SQLITE_CACHE_DIR / "market.sqlite",
        help="market SQLite path, relative to --root unless absolute",
    )
    outcome_parser.add_argument(
        "--out",
        type=Path,
        help="write an optional outcome YAML export; existing paths are never overwritten",
    )
    broker_fact_parser = subparsers.add_parser(
        "broker-fact-draft",
        help="turn a human-reported open/filled/cancelled/expired broker fact into a ledger draft",
    )
    broker_fact_parser.add_argument("--root", type=Path, default=Path.cwd())
    broker_fact_parser.add_argument("--db", type=Path)
    broker_fact_parser.add_argument("--decision-reference")
    broker_fact_parser.add_argument(
        "--direct-fact", action="store_true", help="explicitly record a buy without a proposal"
    )
    broker_fact_parser.add_argument(
        "--status", choices=("open", "filled", "cancelled", "expired"), required=True
    )
    broker_fact_parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    broker_occurrence = broker_fact_parser.add_mutually_exclusive_group(required=True)
    broker_occurrence.add_argument("--occurred-at", type=_datetime_argument)
    broker_occurrence.add_argument(
        "--occurred-on",
        type=_date_argument,
        help="reported date; no exact order or execution time is required",
    )
    broker_fact_parser.add_argument("--ticker")
    broker_fact_parser.add_argument("--quantity", type=int)
    broker_fact_parser.add_argument("--price-yen", type=_decimal_argument)
    broker_fact_parser.add_argument(
        "--reservation-id",
        action="append",
        help="reservation ID; repeat for simultaneous terminal broker facts",
    )
    broker_fact_parser.add_argument("--order-id")
    broker_fact_parser.add_argument(
        "--execution-id",
        help="stable broker fill ID for a filled report; required to distinguish same-day fills",
    )
    broker_fact_parser.add_argument("--sector")
    broker_fact_parser.add_argument("--common-factor", action="append", default=[])
    broker_fact_parser.add_argument("--price-guard-yen", type=_decimal_argument)
    broker_fact_parser.add_argument("--expires-at", type=_datetime_argument)
    broker_fact_parser.add_argument("--expiry-label")
    broker_fact_parser.add_argument("--route-label")
    broker_fact_parser.add_argument("--out", type=Path, required=True)
    sell_parser = subparsers.add_parser(
        "sell-execution-draft",
        help="turn a human-reported sell fill into a FIFO-checked execution ledger draft",
    )
    sell_parser.add_argument("--root", type=Path, default=Path.cwd())
    sell_parser.add_argument("--db", type=Path)
    sell_parser.add_argument("--ticker", required=True)
    sell_parser.add_argument("--quantity", type=int, required=True)
    sell_parser.add_argument("--price-yen", type=_decimal_argument, required=True)
    sell_parser.add_argument("--reservation-id")
    sell_parser.add_argument(
        "--execution-id",
        help="stable broker fill ID; required to distinguish same-day fills",
    )
    sell_occurrence = sell_parser.add_mutually_exclusive_group(required=True)
    sell_occurrence.add_argument("--occurred-at", type=_datetime_argument)
    sell_occurrence.add_argument(
        "--occurred-on",
        type=_date_argument,
        help="fill date only; another same-day fill needs execution-id",
    )
    sell_parser.add_argument("--fees-yen", type=int)
    sell_parser.add_argument("--tax-yen", type=int)
    sell_parser.add_argument("--decision-reference")
    sell_parser.add_argument("--out", type=Path, required=True)
    apply_parser = subparsers.add_parser(
        "apply-draft", help="apply a source-bound ledger draft after explicit human confirmation"
    )
    apply_parser.add_argument("draft", type=Path)
    apply_parser.add_argument("--db", type=Path)
    apply_parser.add_argument("--confirmed", action="store_true")
    event_parser = subparsers.add_parser(
        "event-draft", help="build a draft for one human-reported cash event"
    )
    event_parser.add_argument(
        "--type",
        choices=("contribution", "withdrawal", "income", "cost", "tax_confirmed"),
        required=True,
    )
    event_parser.add_argument("--event-id", required=True)
    event_occurrence = event_parser.add_mutually_exclusive_group(required=True)
    event_occurrence.add_argument("--occurred-at", type=_datetime_argument)
    event_occurrence.add_argument("--occurred-on", type=_date_argument)
    event_parser.add_argument("--amount-yen", type=int, required=True)
    event_parser.add_argument("--ticker")
    event_parser.add_argument("--kind")
    event_parser.add_argument("--db", type=Path)
    event_parser.add_argument("--out", type=Path, required=True)
    override_parser = subparsers.add_parser(
        "override-draft", help="build a draft for one human-approved risk override"
    )
    override_parser.add_argument("--override-id", required=True)
    override_parser.add_argument(
        "--scope",
        choices=("ticker", "sector", "common_factor", "dry_powder"),
        required=True,
    )
    override_parser.add_argument("--key", required=True)
    override_parser.add_argument("--reason", required=True)
    override_parser.add_argument("--decision-reference", required=True)
    override_parser.add_argument("--approved-at", type=_datetime_argument, required=True)
    override_parser.add_argument("--expires-at", type=_datetime_argument, required=True)
    override_parser.add_argument("--db", type=Path)
    override_parser.add_argument("--out", type=Path, required=True)
    meta_parser = subparsers.add_parser(
        "meta-draft", help="build a draft for portfolio estimated-exit-tax settings"
    )
    meta_parser.add_argument("--as-of", type=_datetime_argument, required=True)
    meta_parser.add_argument("--estimated-exit-tax-rate-bps", type=int)
    meta_parser.add_argument(
        "--estimated-exit-tax-basis",
        choices=("ledger_fifo_gross_unrealized_gain",),
    )
    meta_parser.add_argument("--db", type=Path)
    meta_parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ledger":
        try:
            return _emit_ledger(LedgerStoreService(args.db).load(), now=now or datetime.now(JST))
        except (LedgerConflictError, PortfolioLedgerError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "broker-fact-draft":
        return _run_record_broker_fact(args, now=now)
    if args.command == "sell-execution-draft":
        return _run_sell_execution_draft(args)
    if args.command == "apply-draft":
        try:
            result = apply_draft(
                LedgerStoreService(args.db),
                load_draft(args.draft),
                human_confirmed=args.confirmed,
            )
        except (OSError, ValueError, LedgerConflictError, PortfolioLedgerError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        yaml.safe_dump(
            {
                "status": "applied",
                "append_head": result.append_head,
                "event_ids": list(result.event_ids),
            },
            sys.stdout,
            sort_keys=False,
        )
        return 0
    if args.command == "event-draft":
        return _run_event_draft(args)
    if args.command == "override-draft":
        return _run_override_draft(args)
    if args.command == "meta-draft":
        return _run_meta_draft(args)
    if args.command == "outcome":
        benchmark_path = _root_path(args.root, args.benchmark_observation)
        sqlite_path = _root_path(args.root, args.sqlite)
        out_path = None if args.out is None else _root_path(args.root, args.out)
        return _run_outcome(
            benchmark_path,
            sqlite_path,
            out_path,
            db_path=args.db,
        )
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_event_draft(args: argparse.Namespace) -> int:
    common = {
        "event_id": args.event_id,
        "type": args.type,
        "occurred_at": args.occurred_at,
        "amount_yen": args.amount_yen,
    }
    try:
        service = LedgerStoreService(args.db)
        if args.occurred_on is not None:
            common["occurred_at"] = date_only_replay_key(service.load(), args.occurred_on, None)
            common["occurred_on"] = args.occurred_on
        event: HumanEvent
        if args.type == "contribution":
            event = ContributionEvent.model_validate(common)
        elif args.type == "withdrawal":
            event = WithdrawalEvent.model_validate(common)
        elif args.type == "income":
            event = IncomeEvent.model_validate(
                {**common, "ticker": args.ticker, "income_kind": args.kind}
            )
        elif args.type == "cost":
            event = CostEvent.model_validate(
                {**common, "ticker": args.ticker, "cost_kind": args.kind}
            )
        else:
            event = ConfirmedTaxEvent.model_validate(
                {**common, "ticker": args.ticker, "tax_kind": args.kind}
            )
        draft = build_event_draft(service, event)
        write_draft(args.out, draft)
    except (OSError, ValueError, LedgerConflictError, PortfolioLedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {"status": "draft_created", "output": str(args.out), "event_id": args.event_id},
        sys.stdout,
        sort_keys=False,
    )
    return 0


def _run_override_draft(args: argparse.Namespace) -> int:
    try:
        override = HumanOverride.model_validate(
            {
                "override_id": args.override_id,
                "scope": args.scope,
                "key": args.key,
                "reason": args.reason,
                "decision_reference": args.decision_reference,
                "approved_at": args.approved_at.isoformat(),
                "expires_at": args.expires_at.isoformat(),
            }
        )
        draft = build_override_draft(LedgerStoreService(args.db), override)
        write_draft(args.out, draft)
    except (OSError, ValueError, LedgerConflictError, PortfolioLedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {"status": "draft_created", "output": str(args.out), "override_id": args.override_id},
        sys.stdout,
        sort_keys=False,
    )
    return 0


def _run_meta_draft(args: argparse.Namespace) -> int:
    if (args.estimated_exit_tax_rate_bps is None) != (args.estimated_exit_tax_basis is None):
        print("error: exit tax rate and basis must be specified together", file=sys.stderr)
        return 2
    try:
        draft = build_meta_draft(
            LedgerStoreService(args.db),
            as_of=args.as_of,
            estimated_exit_tax_rate_bps=args.estimated_exit_tax_rate_bps,
            estimated_exit_tax_basis=args.estimated_exit_tax_basis,
        )
        write_draft(args.out, draft)
    except (OSError, ValueError, LedgerConflictError, PortfolioLedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {"status": "draft_created", "output": str(args.out)},
        sys.stdout,
        sort_keys=False,
    )
    return 0


def _emit_ledger(document: PortfolioLedgerDocument, *, now: datetime) -> int:
    try:
        snapshot = current_portfolio(document, sqlite_path=MARKET_DB_PATH, now=now)
    except PortfolioLedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    payload = snapshot_to_payload(snapshot)
    migration_event_count = sum(
        event.event_id.startswith("migration-") for event in document.events
    )
    payload["event_annotations"] = (
        [
            {
                "code": "ledger.migration-initialization",
                "event_count": migration_event_count,
                "message": (
                    "migration events initialize canonical state and are not "
                    "human-reported broker facts"
                ),
            }
        ]
        if migration_event_count
        else []
    )
    yaml.safe_dump(
        payload,
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return 0


def _run_outcome(
    benchmark_path: Path,
    sqlite_path: Path,
    out_path: Path | None,
    *,
    db_path: Path | None = None,
) -> int:
    """Emit TWR from one market snapshot without fetching data at runtime."""

    try:
        benchmark = load_benchmark_observation(benchmark_path)
    except BenchmarkObservationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        ledger = LedgerStoreService(db_path).load()
        # An outcome is written once and never corrected, so it must not be derived
        # from a ledger the current snapshot would refuse. Historical valuation
        # tolerates a reservation still awaiting its release while replaying a prefix;
        # that tolerance must not extend to publishing from a ledger whose end state a
        # human has yet to resolve.
        require_resolved_expiries(replay_events_through(ledger.events, ledger.as_of))
    except (LedgerConflictError, PortfolioLedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    tickers = tuple(
        sorted(
            {
                event.ticker
                for event in ledger.events
                if isinstance(event, (ExecutionEvent, ReservationEvent))
            }
        )
    )
    connection = connect_read_only(sqlite_path) if sqlite_path.exists() else None
    try:
        if connection is None:
            calendar_days, bars = None, None
        else:
            connection.execute("BEGIN")
            calendar_days, bars = read_portfolio_market_inputs(
                sqlite_path,
                tickers,
                benchmark.period_start_date,
                benchmark.period_end_date,
                connection=connection,
            )
        if calendar_days is None:
            payload = _outcome_unresolved_payload(benchmark, "benchmark_unavailable")
        elif bars is None:
            payload = _outcome_unresolved_payload(benchmark, "missing_market_price")
        else:
            assert connection is not None
            outcome = compute_portfolio_outcome(
                ledger,
                benchmark,
                business_days=tuple(day.day for day in calendar_days if day.is_business_day),
                bars=tuple(bars),
                confirm_price_basis=lambda ticker, first, last: market_basis_is_confirmed(
                    basis="price",
                    sqlite_path=sqlite_path,
                    ticker=ticker,
                    from_date=first,
                    through_date=last,
                    connection=connection,
                ),
            )
            payload = outcome_to_payload(outcome)
    finally:
        if connection is not None:
            connection.close()
    payload.update(
        benchmark_id=benchmark.benchmark_id,
        market_data_ref=str(sqlite_path),
        market_data_coverage_start_date=benchmark.period_start_date.isoformat(),
        market_data_coverage_end_date=benchmark.period_end_date.isoformat(),
    )
    payload["benchmark_observation"] = benchmark.model_dump(mode="json")
    outcome_id = f"outcome-{benchmark.horizon}-{benchmark.period_end_date.isoformat()}"
    if out_path is not None and out_path.exists():
        print(f"error: refusing to overwrite existing outcome: {out_path}", file=sys.stderr)
        return 2
    if payload["status"] == "resolved":
        try:
            PortfolioOutcomeStore(db_path).publish(
                PortfolioOutcomePublication(
                    outcome_id=outcome_id,
                    horizon=benchmark.horizon,
                    period_start_date=benchmark.period_start_date.isoformat(),
                    period_end_date=benchmark.period_end_date.isoformat(),
                    status="resolved",
                    payload=payload,
                )
            )
        except (ValueError, sqlite3.Error) as error:
            print(f"error: failed to publish portfolio outcome: {error}", file=sys.stderr)
            return 2
        payload["outcome_id"] = outcome_id
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    return 0


def _outcome_unresolved_payload(benchmark: BenchmarkObservation, reason: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "portfolio_outcome",
        "status": "unresolved",
        "reason": reason,
        "horizon": benchmark.horizon,
        "period_start_date": benchmark.period_start_date.isoformat(),
        "period_end_date": benchmark.period_end_date.isoformat(),
    }


def _run_record_broker_fact(args: argparse.Namespace, *, now: datetime | None) -> int:
    try:
        ledger_service = LedgerStoreService(args.db)
        draft, event_ids = build_broker_fact_draft(
            ledger_service,
            partial(resolve_buy_reference, db_path=args.db),
            decision_reference=args.decision_reference,
            status=args.status,
            direct_fact=args.direct_fact,
            side=args.side,
            occurred_at=args.occurred_at,
            occurred_on=args.occurred_on,
            ticker=args.ticker,
            quantity=args.quantity,
            price_yen=args.price_yen,
            reservation_ids=tuple(args.reservation_id or ()),
            order_id=args.order_id,
            execution_id=args.execution_id,
            sector=args.sector,
            common_factors=tuple(sorted(set(args.common_factor))),
            price_guard_yen=args.price_guard_yen,
            expires_at=args.expires_at,
            expiry_label=args.expiry_label,
            route_label=args.route_label,
            now=now,
        )
        if draft is not None:
            output_path = _root_path(args.root, args.out)
            write_draft(output_path, draft)
        else:
            output_path = None
        reference = args.decision_reference
        if draft is not None:
            if draft.binding_addition is not None:
                reference = draft.binding_addition.reference
            elif reference is None:
                reference = next(
                    getattr(event, "decision_reference", None)
                    for event in draft.replacement.events
                    if event.event_id in event_ids
                    and getattr(event, "decision_reference", None) is not None
                )
        elif reference is None:
            known = known_broker_reference(
                ledger_service.load(),
                execution_id=args.execution_id,
                order_id=args.order_id,
                reservation_ids=tuple(args.reservation_id or ()),
            )
            if known is None:
                raise ValueError("saved broker identity is unavailable; prepare again")
            reference = known[0]
    except (OSError, PortfolioLedgerError, BrokerFactRecordingError, ValueError) as error:
        print(f"error: failed to build broker fact draft: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {
            "status": "draft_created" if draft is not None else "no_change",
            "decision_reference": reference,
            "output": None if output_path is None else str(output_path),
            "event_ids": list(event_ids),
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


def _run_sell_execution_draft(args: argparse.Namespace) -> int:
    source_event_ids: set[str] = set()
    try:
        draft = build_sell_execution_draft(
            LedgerStoreService(args.db),
            occurred_at=args.occurred_at,
            occurred_on=args.occurred_on,
            ticker=args.ticker,
            quantity=args.quantity,
            price_yen=args.price_yen,
            reservation_id=args.reservation_id,
            fees_yen=args.fees_yen,
            tax_yen=args.tax_yen,
            decision_reference=args.decision_reference,
            execution_id=args.execution_id,
        )
        if draft is not None:
            output_path = _root_path(args.root, args.out)
            source_event_ids = {event.event_id for event in draft.source.events}
            write_draft(output_path, draft)
        else:
            output_path = None
    except (OSError, LedgerConflictError, PortfolioLedgerError, ValueError) as error:
        print(f"error: failed to build sell execution draft: {error}", file=sys.stderr)
        return 2
    event_ids = (
        [
            event.event_id
            for event in draft.replacement.events
            if event.event_id not in source_event_ids
        ]
        if draft is not None
        else []
    )
    yaml.safe_dump(
        {
            "status": "draft_created" if draft is not None else "no_change",
            "output": None if output_path is None else str(output_path),
            "ticker": args.ticker,
            "event_ids": event_ids,
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


def _root_path(root: Path, path: Path) -> Path:
    """Resolve explicit CLI paths consistently; writers own overwrite protection."""
    return path if path.is_absolute() else root / path


if __name__ == "__main__":
    raise SystemExit(main())
