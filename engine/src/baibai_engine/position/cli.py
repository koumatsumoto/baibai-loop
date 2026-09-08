"""Command-line entry for application-DB portfolio records.

Composes ledger, position-review, and outcome inputs. This entry point owns
SQLite composition while position core modules remain free of screening
dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_engine.market.jpx_total_return import (
    BenchmarkObservation,
    BenchmarkObservationError,
    load_benchmark_observation,
)
from baibai_engine.market.store import read_daily_bars_for_tickers, read_market_calendar
from baibai_engine.position.broker_fact_recording import BrokerFactRecordingError
from baibai_engine.position.drafts import (
    HumanEvent,
    apply_draft,
    build_event_draft,
    build_meta_draft,
    build_override_draft,
    build_sell_execution_draft,
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
from baibai_engine.position.outcome import compute_portfolio_outcome, outcome_to_payload
from baibai_engine.position.outcome_store import (
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.position.valuation import current_portfolio
from baibai_engine.research.broker_fact_service import build_broker_fact_draft
from baibai_engine.research.capital_allocation_service import (
    CapitalAllocationAssessmentService,
)
from baibai_engine.research.position_review import PositionReviewDocument
from baibai_engine.research.position_review_service import PositionReviewService


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
    position_review_parser = subparsers.add_parser(
        "position-review",
        description=(
            "Recompute a Position Review draft's hold/exit from thesis "
            "and the remaining economic reward, and emit a YAML "
            "summary. A broken thesis is the priority sell candidate; fair value is "
            "a review trigger; price decline alone is never an exit reason."
        ),
        help="recompute hold/exit from a Position Review draft YAML",
    )
    position_review_parser.add_argument(
        "--input",
        type=Path,
        help="Position Review draft YAML path",
    )
    position_review_parser.add_argument(
        "operation",
        nargs="?",
        choices=("publish",),
        help="publish a human-confirmed draft to the application DB",
    )
    position_review_parser.add_argument("draft", nargs="?", type=Path)
    position_review_parser.add_argument("--root", type=Path, default=Path.cwd())
    position_review_parser.add_argument("--db", type=Path)
    position_review_parser.add_argument("--position-review-id")
    position_review_parser.add_argument("--thesis-id")
    position_review_parser.add_argument("--confirmed", action="store_true")
    position_review_parser.add_argument("--sqlite", type=Path, default=MARKET_DB_PATH)
    holding_build_parser = subparsers.add_parser(
        "position-review-build",
        help="build a Position Review draft from a ready thesis, its review, and the ledger",
    )
    holding_build_parser.add_argument("--root", type=Path, default=Path.cwd())
    holding_build_parser.add_argument("--db", type=Path)
    holding_build_parser.add_argument("--thesis-id", required=True)
    holding_build_parser.add_argument("--position-id", required=True)
    holding_build_parser.add_argument("--sqlite", type=Path, default=MARKET_DB_PATH)
    holding_build_parser.add_argument("--out", type=Path, required=True)
    broker_fact_parser = subparsers.add_parser(
        "broker-fact-draft",
        help="turn a human-reported open/filled/cancelled/expired broker fact into a ledger draft",
    )
    broker_fact_parser.add_argument("--root", type=Path, default=Path.cwd())
    broker_fact_parser.add_argument("--db", type=Path)
    broker_fact_parser.add_argument("--decision-reference", required=True)
    broker_fact_parser.add_argument(
        "--status", choices=("open", "filled", "cancelled", "expired"), required=True
    )
    broker_fact_parser.add_argument("--occurred-at", type=_datetime_argument, required=True)
    broker_fact_parser.add_argument("--ticker")
    broker_fact_parser.add_argument("--quantity", type=int)
    broker_fact_parser.add_argument("--price-yen", type=_decimal_argument)
    broker_fact_parser.add_argument(
        "--reservation-id",
        action="append",
        help="reservation ID; repeat for simultaneous terminal broker facts",
    )
    broker_fact_parser.add_argument("--order-id")
    broker_fact_parser.add_argument("--sector")
    broker_fact_parser.add_argument("--common-factor", action="append", default=[])
    broker_fact_parser.add_argument("--price-guard-yen", type=_decimal_argument)
    broker_fact_parser.add_argument("--expires-at", type=_datetime_argument)
    broker_fact_parser.add_argument("--ordered-at", type=_datetime_argument)
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
    sell_parser.add_argument("--occurred-at", type=_datetime_argument, required=True)
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
    event_parser.add_argument("--occurred-at", type=_datetime_argument, required=True)
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
    if args.command == "position-review":
        if args.operation == "publish":
            if args.draft is None or args.thesis_id is None:
                print(
                    "error: position-review publish requires <draft> and --thesis-id",
                    file=sys.stderr,
                )
                return 2
            return _run_position_review_publish(args, now=now)
        if args.input is None:
            print("error: position-review validation requires --input", file=sys.stderr)
            return 2
        input_path = args.input if args.input.is_absolute() else args.root / args.input
        return _run_position_review(input_path, db_path=args.db, sqlite_path=args.sqlite, now=now)
    if args.command == "position-review-build":
        return _run_position_review_build_db(
            db_path=args.db,
            thesis_id=args.thesis_id,
            sqlite_path=args.sqlite,
            position_id=args.position_id,
            root=args.root,
            out=args.out,
            now=now,
        )
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
        benchmark_path = (
            args.benchmark_observation
            if args.benchmark_observation.is_absolute()
            else args.root / args.benchmark_observation
        )
        sqlite_path = args.sqlite if args.sqlite.is_absolute() else args.root / args.sqlite
        out_path = (
            None
            if args.out is None
            else (args.out if args.out.is_absolute() else args.root / args.out)
        )
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
        draft = build_event_draft(LedgerStoreService(args.db), event)
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
    """Emit a source-bound TWR result without fetching market data at runtime."""

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
    calendar_days = read_market_calendar(
        sqlite_path, benchmark.period_start_date, benchmark.period_end_date
    )
    if calendar_days is None:
        payload = _outcome_unresolved_payload(benchmark, "benchmark_unavailable")
        market_fingerprint = None
    else:
        tickers = tuple(
            sorted(
                {
                    event.ticker
                    for event in ledger.events
                    if isinstance(event, (ExecutionEvent, ReservationEvent))
                }
            )
        )
        bars = read_daily_bars_for_tickers(
            sqlite_path, tickers, benchmark.period_start_date, benchmark.period_end_date
        )
        if bars is None:
            payload = _outcome_unresolved_payload(benchmark, "missing_market_price")
            market_fingerprint = None
        else:
            outcome = compute_portfolio_outcome(
                ledger,
                benchmark,
                business_days=tuple(day.day for day in calendar_days if day.is_business_day),
                bars=tuple(bars),
            )
            payload = outcome_to_payload(outcome)
            market_fingerprint = _market_data_fingerprint(bars)
    payload.update(
        benchmark_id=benchmark.benchmark_id,
        market_data_ref=str(sqlite_path),
        market_data_sha256=_sha256(sqlite_path) if sqlite_path.is_file() else None,
        market_data_coverage_start_date=benchmark.period_start_date.isoformat(),
        market_data_coverage_end_date=benchmark.period_end_date.isoformat(),
        market_data_fingerprint=market_fingerprint,
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
        "schema_version": 1,
        "kind": "portfolio_outcome",
        "status": "unresolved",
        "reason": reason,
        "horizon": benchmark.horizon,
        "period_start_date": benchmark.period_start_date.isoformat(),
        "period_end_date": benchmark.period_end_date.isoformat(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _market_data_fingerprint(bars: list[JQuantsDailyBar]) -> str:
    rows = [
        (
            bar.ticker,
            bar.traded_at.isoformat(),
            bar.close,
            bar.adjustment_factor,
        )
        for bar in bars
    ]
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _run_position_review(
    path: Path, *, db_path: Path | None, sqlite_path: Path, now: datetime | None
) -> int:
    try:
        document = PositionReviewDocument.model_validate(safe_load(path.read_text()))
        result = PositionReviewService(
            db_path, sqlite_path=sqlite_path, clock=lambda: now or datetime.now(JST)
        ).check(document)
        yaml.safe_dump(asdict(result), sys.stdout, allow_unicode=True)
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _run_position_review_publish(args: argparse.Namespace, *, now: datetime | None) -> int:
    try:
        path = args.draft if args.draft.is_absolute() else args.root / args.draft
        document = PositionReviewDocument.model_validate(safe_load(path.read_text()))
        if document.thesis_id != args.thesis_id or (
            args.position_review_id and document.position_review_id != args.position_review_id
        ):
            raise ValueError("CLI identity differs from submitted draft")
        document = PositionReviewService(
            args.db, sqlite_path=args.sqlite, clock=lambda: now or datetime.now(JST)
        ).publish(document, confirmed=args.confirmed)
        yaml.safe_dump(
            {"position_review_id": document.position_review_id, "action": document.action},
            sys.stdout,
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _run_position_review_build_db(
    *,
    db_path: Path | None,
    thesis_id: str,
    sqlite_path: Path,
    position_id: str,
    root: Path,
    out: Path,
    now: datetime | None,
) -> int:
    try:
        path = _draft_output_path(root, out, label="Position Review")
        service = PositionReviewService(
            db_path, sqlite_path=sqlite_path, clock=lambda: now or datetime.now(JST)
        )
        document = service.build(thesis_id=thesis_id, position_id=position_id)
        evaluation = service.check(document)
        payload = document.model_dump(mode="json")
        _write_yaml_exclusive(path, payload)
        yaml.safe_dump(
            {**payload, "current_price_projection": evaluation.current_price_projection},
            sys.stdout,
            allow_unicode=True,
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _run_record_broker_fact(args: argparse.Namespace, *, now: datetime | None) -> int:
    try:
        draft, event_ids = build_broker_fact_draft(
            LedgerStoreService(args.db),
            CapitalAllocationAssessmentService(args.db),
            decision_reference=args.decision_reference,
            status=args.status,
            occurred_at=args.occurred_at,
            ticker=args.ticker,
            quantity=args.quantity,
            price_yen=args.price_yen,
            reservation_ids=tuple(args.reservation_id or ()),
            order_id=args.order_id,
            sector=args.sector,
            common_factors=tuple(sorted(set(args.common_factor))),
            price_guard_yen=args.price_guard_yen,
            expires_at=args.expires_at,
            ordered_at=args.ordered_at,
            now=now,
        )
        if draft is not None:
            output_path = _draft_output_path(args.root, args.out, label="ledger draft")
            write_draft(output_path, draft)
        else:
            output_path = None
    except (OSError, PortfolioLedgerError, BrokerFactRecordingError, ValueError) as error:
        print(f"error: failed to build broker fact draft: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {
            "status": "draft_created" if draft is not None else "no_change",
            "decision_reference": args.decision_reference,
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
        output_path = _draft_output_path(args.root, args.out, label="sell execution ledger draft")
        draft = build_sell_execution_draft(
            LedgerStoreService(args.db),
            occurred_at=args.occurred_at,
            ticker=args.ticker,
            quantity=args.quantity,
            price_yen=args.price_yen,
            fees_yen=args.fees_yen,
            tax_yen=args.tax_yen,
            decision_reference=args.decision_reference,
        )
        source_event_ids = {event.event_id for event in draft.source.events}
        write_draft(output_path, draft)
    except (OSError, LedgerConflictError, PortfolioLedgerError, ValueError) as error:
        print(f"error: failed to build sell execution draft: {error}", file=sys.stderr)
        return 2
    event_ids = [
        event.event_id
        for event in draft.replacement.events
        if event.event_id not in source_event_ids
    ]
    yaml.safe_dump(
        {
            "status": "draft_created",
            "output": str(output_path),
            "ticker": args.ticker,
            "event_ids": event_ids,
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


def _draft_output_path(root: Path, output: Path, *, label: str) -> Path:
    """Resolve a new draft below root without following repository symlinks."""

    if output.is_absolute() or ".." in output.parts:
        raise ValueError(f"{label} --out must be a relative path below --root")
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"--root is not a directory: {root}")
    candidate = resolved_root / output
    current = resolved_root
    for part in output.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} --out must not traverse a symlink: {output}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"{label} --out escapes --root: {output}") from error
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing {label}: {resolved}")
    return resolved


def _write_yaml_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent:
        raise OSError(f"draft parent became a symlink: {path.parent}")
    with path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(payload, stream, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    raise SystemExit(main())
