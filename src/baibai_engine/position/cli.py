"""Command-line entry for repository portfolio records.

Composes ledger, holding-review, and outcome inputs. This entry point owns
SQLite composition while position core modules remain free of screening
dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_engine.market.jpx_total_return import (
    BenchmarkObservation,
    BenchmarkObservationError,
    load_benchmark_observation,
)
from baibai_engine.market.sqlite import (
    connect_current,
    daily_bars_covered_by_data,
    optional_float,
    range_covered,
)
from baibai_engine.market.store import read_daily_bars_for_tickers, read_market_calendar
from baibai_engine.position.drafts import (
    HumanEvent,
    apply_draft,
    build_event_draft,
    build_meta_draft,
    build_override_draft,
    load_draft,
    write_draft,
)
from baibai_engine.position.holding_review import (
    HoldingReviewDocument,
    HoldingReviewError,
    evaluate_holding_review,
    load_holding_review,
    result_to_payload,
    validate_holding_review_sources,
)
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    ExecutionEvent,
    HumanOverride,
    IncomeEvent,
    MarketPrice,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    ReservationEvent,
    WithdrawalEvent,
    load_portfolio_ledger,
    load_portfolio_ledger_with_sha256,
    reconcile_portfolio,
    replay_events_through,
    snapshot_to_payload,
)
from baibai_engine.position.outcome import compute_portfolio_outcome, outcome_to_payload
from baibai_engine.position.outcome_store import (
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.position.result_recording import ResultRecordingError, record_result
from baibai_engine.position.result_service import build_result_draft
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService
from baibai_engine.proposals.store import ProposalStoreService
from baibai_engine.research.holding_review_builder import (
    build_holding_review,
    build_holding_review_from_db,
    validate_holding_review_scalars,
)


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
        description="Reconcile the repository-only portfolio ledger and emit a YAML snapshot.",
        help="reconcile available cash, reservations, holdings, income, costs, and warnings",
    )
    ledger_parser.add_argument("--root", type=Path, default=Path.cwd())
    ledger_parser.add_argument("--db", type=Path)
    ledger_parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("records/04-position/portfolio-ledger.yaml"),
        help="ledger YAML path, relative to --root unless absolute",
    )
    outcome_parser = subparsers.add_parser(
        "outcome",
        help=(
            "report a portfolio outcome only from a canonical ledger and JPX "
            "gross-total-return observation"
        ),
    )
    outcome_parser.add_argument("--root", type=Path, default=Path.cwd())
    outcome_parser.add_argument("--db", type=Path)
    outcome_parser.add_argument(
        "--ledger", type=Path, default=Path("records/04-position/portfolio-ledger.yaml")
    )
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
        help="write a new canonical outcome YAML; existing paths are never overwritten",
    )
    holding_review_parser = subparsers.add_parser(
        "holding-review",
        description=(
            "Recompute a holding review draft's hold/add/reduce/exit from thesis "
            "health and the after-tax replacement comparison, and emit a YAML "
            "summary. A broken thesis is the priority sell candidate; fair value is "
            "a review trigger; price decline alone is never an exit reason."
        ),
        help="recompute hold/add/reduce/exit from a holding review draft YAML",
    )
    holding_review_parser.add_argument(
        "--input",
        type=Path,
        help="holding review draft YAML path",
    )
    holding_review_parser.add_argument(
        "operation",
        nargs="?",
        choices=("publish",),
        help="publish a human-confirmed draft to the application DB",
    )
    holding_review_parser.add_argument("draft", nargs="?", type=Path)
    holding_review_parser.add_argument("--root", type=Path, default=Path.cwd())
    holding_review_parser.add_argument("--db", type=Path)
    holding_review_parser.add_argument("--holding-review-id")
    holding_review_parser.add_argument("--packet-id")
    holding_review_parser.add_argument("--candidate-packet-id")
    holding_build_parser = subparsers.add_parser(
        "holding-review-build",
        help="build a holding review draft from a ready packet, its review, and the ledger",
    )
    holding_build_parser.add_argument("--root", type=Path, default=Path.cwd())
    holding_build_parser.add_argument("--packet", type=Path)
    holding_build_parser.add_argument("--ledger", type=Path)
    holding_build_parser.add_argument("--db", type=Path)
    holding_build_parser.add_argument("--packet-id")
    holding_build_parser.add_argument("--position-id", required=True)
    holding_build_parser.add_argument("--candidate-packet", type=Path)
    holding_build_parser.add_argument("--candidate-packet-id")
    holding_build_parser.add_argument("--out", type=Path, required=True)
    market_price_parser = subparsers.add_parser(
        "market-price-draft",
        help="build a new ledger draft with exact same-day raw closes for all open holdings",
    )
    market_price_parser.add_argument("--root", type=Path, default=Path.cwd())
    market_price_parser.add_argument("--ledger", type=Path)
    market_price_parser.add_argument("--db", type=Path)
    market_price_parser.add_argument("--sqlite", type=Path, required=True)
    market_price_parser.add_argument("--asof", type=_date_argument, required=True)
    market_price_parser.add_argument("--out", type=Path, required=True)
    result_parser = subparsers.add_parser(
        "record-result",
        help="turn a human-reported open/filled/cancelled/expired result into a ledger draft",
    )
    result_parser.add_argument("--root", type=Path, default=Path.cwd())
    result_parser.add_argument("--ledger", type=Path)
    result_parser.add_argument("--db", type=Path)
    result_parser.add_argument("--proposal-ref", required=True)
    result_parser.add_argument(
        "--status", choices=("open", "filled", "cancelled", "expired"), required=True
    )
    result_parser.add_argument("--occurred-at", type=_datetime_argument, required=True)
    result_parser.add_argument("--ticker")
    result_parser.add_argument("--quantity", type=int)
    result_parser.add_argument("--price-yen", type=_decimal_argument)
    result_parser.add_argument("--reservation-id")
    result_parser.add_argument("--order-id")
    result_parser.add_argument("--sector")
    result_parser.add_argument("--common-factor", action="append", default=[])
    result_parser.add_argument("--price-guard-yen", type=_decimal_argument)
    result_parser.add_argument("--expires-at", type=_datetime_argument)
    result_parser.add_argument("--approved-at", type=_datetime_argument)
    result_parser.add_argument("--out", type=Path, required=True)
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
        if args.db is not None:
            try:
                return _emit_ledger(LedgerStoreService(args.db).load())
            except (LedgerConflictError, PortfolioLedgerError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
        ledger_path = args.ledger if args.ledger.is_absolute() else args.root / args.ledger
        return _run_ledger(ledger_path)
    if args.command == "holding-review":
        if args.operation == "publish":
            if args.draft is None or args.packet_id is None:
                print(
                    "error: holding-review publish requires <draft> and --packet-id",
                    file=sys.stderr,
                )
                return 2
            return _run_holding_review_publish(args)
        if args.input is None:
            print("error: holding-review validation requires --input", file=sys.stderr)
            return 2
        input_path = args.input if args.input.is_absolute() else args.root / args.input
        return _run_holding_review(input_path, root=args.root)
    if args.command == "holding-review-build":
        if args.db is not None:
            if args.packet_id is None:
                print("error: DB holding-review-build requires --packet-id", file=sys.stderr)
                return 2
            return _run_holding_review_build_db(
                db_path=args.db,
                packet_id=args.packet_id,
                candidate_packet_id=args.candidate_packet_id,
                position_id=args.position_id,
                root=args.root,
                out=args.out,
            )
        if args.packet is None or args.ledger is None:
            print("error: holding-review-build requires --db", file=sys.stderr)
            return 2
        return _run_holding_review_build(
            root=args.root,
            packet=args.packet,
            ledger=args.ledger,
            position_id=args.position_id,
            candidate_packet=args.candidate_packet,
            out=args.out,
        )
    if args.command == "market-price-draft":
        if args.db is None and args.ledger is None:
            print("error: market-price-draft requires --db", file=sys.stderr)
            return 2
        return _run_market_price_draft(
            root=args.root,
            ledger=args.ledger,
            sqlite_path=args.sqlite,
            asof=args.asof,
            out=args.out,
            db_path=args.db,
        )
    if args.command == "record-result":
        return _run_record_result(args, now=now)
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
        ledger_path = args.ledger if args.ledger.is_absolute() else args.root / args.ledger
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
            ledger_path,
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


def _run_ledger(path: Path) -> int:
    try:
        document = load_portfolio_ledger(path)
    except PortfolioLedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return _emit_ledger(document)


def _emit_ledger(document: PortfolioLedgerDocument) -> int:
    try:
        snapshot = reconcile_portfolio(document)
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
                    "human-reported broker results"
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
    ledger_path: Path,
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
    if db_path is None and not ledger_path.is_file():
        payload = {
            "schema_version": 1,
            "kind": "portfolio_outcome",
            "status": "unresolved",
            "reason": "activation_pending",
            "benchmark_id": benchmark.benchmark_id,
            "horizon": benchmark.horizon,
        }
        if out_path is not None:
            print("error: activation_pending outcome cannot be persisted", file=sys.stderr)
            return 2
        yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
        return 0
    try:
        ledger = (
            LedgerStoreService(db_path).load()
            if db_path is not None
            else load_portfolio_ledger(ledger_path)
        )
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
    if db_path is not None:
        payload["benchmark_observation"] = benchmark.model_dump(mode="json")
        outcome_id = f"outcome-{benchmark.horizon}-{benchmark.period_end_date.isoformat()}"
        try:
            PortfolioOutcomeStore(db_path).publish(
                PortfolioOutcomePublication(
                    outcome_id=outcome_id,
                    horizon=benchmark.horizon,
                    period_start_date=benchmark.period_start_date.isoformat(),
                    period_end_date=benchmark.period_end_date.isoformat(),
                    status=str(payload["status"]),
                    payload=payload,
                )
            )
        except (ValueError, sqlite3.Error) as error:
            print(f"error: failed to publish portfolio outcome: {error}", file=sys.stderr)
            return 2
        payload["outcome_id"] = outcome_id
    else:
        payload.update(
            benchmark_observation_ref=str(benchmark_path),
            benchmark_observation_sha256=_sha256(benchmark_path),
            ledger_ref=str(ledger_path),
            ledger_sha256=_sha256(ledger_path),
        )
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    if out_path is not None:
        if out_path.exists():
            print(f"error: refusing to overwrite existing outcome: {out_path}", file=sys.stderr)
            return 2
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


def _run_holding_review(path: Path, *, root: Path) -> int:
    try:
        document = load_holding_review(path)
    except HoldingReviewError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        validate_holding_review_sources(document, root=root)
        validate_holding_review_scalars(document, root=root)
    except HoldingReviewError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    result = evaluate_holding_review(document)
    yaml.safe_dump(
        result_to_payload(document, result),
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for finding in result.errors:
        print(f"error: {finding}", file=sys.stderr)
    return 2 if result.errors else 0


def _run_holding_review_publish(args: argparse.Namespace) -> int:
    from baibai_engine.foundation.yaml_io import safe_load
    from baibai_engine.research.store import ResearchStoreService, ResearchValidationError

    draft_path = args.draft if args.draft.is_absolute() else args.root / args.draft
    try:
        raw = safe_load(draft_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ResearchValidationError("holding review draft must be a mapping")
        document = HoldingReviewDocument.model_validate(raw)
        holding_review_id = args.holding_review_id or (
            f"holding-review-{document.as_of:%Y%m%d}-{document.ticker}-{document.position_id}"
        )
        ResearchStoreService(args.db).publish_holding_review(
            holding_review_id,
            args.packet_id,
            raw,
            root=args.root,
            candidate_packet_id=args.candidate_packet_id,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        {
            "holding_review_id": holding_review_id,
            "packet_id": args.packet_id,
            "candidate_packet_id": args.candidate_packet_id,
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


def _run_holding_review_build(
    *,
    root: Path,
    packet: Path,
    ledger: Path,
    position_id: str,
    candidate_packet: Path | None,
    out: Path,
) -> int:
    try:
        output_path = _draft_output_path(root, out, label="holding review")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        document = build_holding_review(
            root=root,
            ledger_ref=ledger,
            holding_packet_ref=packet,
            position_id=position_id,
            candidate_packet_ref=candidate_packet,
        )
        result = evaluate_holding_review(document)
    except (HoldingReviewError, PortfolioLedgerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if result.errors:
        for finding in result.errors:
            print(f"error: {finding}", file=sys.stderr)
        return 2
    payload = document.model_dump(mode="json")
    try:
        _write_yaml_exclusive(output_path, payload)
    except OSError as error:
        print(f"error: failed to write holding review draft: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def _run_holding_review_build_db(
    *,
    db_path: Path,
    packet_id: str,
    candidate_packet_id: str | None,
    position_id: str,
    root: Path,
    out: Path,
) -> int:
    try:
        output_path = _draft_output_path(root, out, label="holding review")
        document = build_holding_review_from_db(
            db_path=db_path,
            holding_packet_id=packet_id,
            candidate_packet_id=candidate_packet_id,
            position_id=position_id,
        )
        result = evaluate_holding_review(document)
        if result.errors:
            raise HoldingReviewError("; ".join(result.errors))
        payload = document.model_dump(mode="json")
        _write_yaml_exclusive(output_path, payload)
    except (OSError, HoldingReviewError, PortfolioLedgerError, ValueError) as error:
        print(f"error: failed to build DB holding review: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def _run_record_result(args: argparse.Namespace, *, now: datetime | None) -> int:
    if args.db is not None:
        try:
            draft, event_ids = build_result_draft(
                LedgerStoreService(args.db),
                ProposalStoreService(args.db),
                proposal_id=args.proposal_ref,
                status=args.status,
                occurred_at=args.occurred_at,
                ticker=args.ticker,
                quantity=args.quantity,
                price_yen=args.price_yen,
                reservation_id=args.reservation_id,
                order_id=args.order_id,
                sector=args.sector,
                common_factors=tuple(sorted(set(args.common_factor))),
                price_guard_yen=args.price_guard_yen,
                expires_at=args.expires_at,
                approved_at=args.approved_at,
                now=now,
            )
            if draft is not None:
                db_output_path = _draft_output_path(args.root, args.out, label="ledger draft")
                write_draft(db_output_path, draft)
            else:
                db_output_path = None
        except (OSError, PortfolioLedgerError, ResultRecordingError, ValueError) as error:
            print(f"error: failed to build broker result draft: {error}", file=sys.stderr)
            return 2
        yaml.safe_dump(
            {
                "status": "draft_created" if draft is not None else "no_change",
                "proposal_id": args.proposal_ref,
                "output": None if db_output_path is None else str(db_output_path),
                "event_ids": list(event_ids),
            },
            sys.stdout,
            sort_keys=False,
            allow_unicode=True,
        )
        return 0
    if args.ledger is None:
        print("error: record-result requires --db", file=sys.stderr)
        return 2
    ledger_path = args.ledger if args.ledger.is_absolute() else args.root / args.ledger
    try:
        ledger, source_sha256 = load_portfolio_ledger_with_sha256(ledger_path)
        result = record_result(
            ledger,
            proposal_ref=args.proposal_ref,
            status=args.status,
            occurred_at=args.occurred_at,
            ticker=args.ticker,
            quantity=args.quantity,
            price_yen=args.price_yen,
            reservation_id=args.reservation_id,
            order_id=args.order_id,
            sector=args.sector,
            common_factors=tuple(sorted(set(args.common_factor))),
            price_guard_yen=args.price_guard_yen,
            expires_at=args.expires_at,
            approved_at=args.approved_at,
            now=now,
        )
    except (PortfolioLedgerError, ResultRecordingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    output_path: Path | None = None
    if result.changed:
        try:
            output_path = _draft_output_path(args.root, args.out, label="ledger draft")
            _write_yaml_exclusive(output_path, result.document.model_dump(mode="json"))
        except (OSError, ValueError) as error:
            print(f"error: failed to write ledger draft: {error}", file=sys.stderr)
            return 2
    yaml.safe_dump(
        {
            "status": "draft_created" if result.changed else "no_change",
            "source_ledger": str(ledger_path),
            "source_ledger_sha256": source_sha256,
            "output": str(output_path) if output_path is not None else None,
            "event_ids": list(result.event_ids),
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


class _MarketPriceDraftError(ValueError):
    """The requested market-price observation cannot produce a complete draft."""


def _run_market_price_draft(
    *,
    root: Path,
    ledger: Path | None,
    sqlite_path: Path,
    asof: date,
    out: Path,
    db_path: Path | None = None,
) -> int:
    ledger_path = None if ledger is None else (ledger if ledger.is_absolute() else root / ledger)
    resolved_sqlite = sqlite_path if sqlite_path.is_absolute() else root / sqlite_path
    try:
        output_path = _draft_output_path(root, out, label="market price ledger draft")
        sqlite_ref = _repository_source_ref(root, resolved_sqlite)
        if db_path is None:
            assert ledger_path is not None
            document, source_sha256 = load_portfolio_ledger_with_sha256(ledger_path)
            expected_head = None
        else:
            service = LedgerStoreService(db_path)
            document = service.load()
            source_sha256 = None
            expected_head = service.append_head()
        state = replay_events_through(document.events, document.as_of)
        tickers = tuple(
            sorted(
                ticker
                for ticker, lots in state.lots.items()
                if any(lot.quantity > 0 for lot in lots)
            )
        )
        if not tickers:
            raise _MarketPriceDraftError("ledger has no open holdings to price")
        current_prices = {price.ticker: price for price in document.market_prices}
        newer_prices = sorted(
            ticker
            for ticker in tickers
            if (current_price := current_prices.get(ticker)) is not None
            and current_price.observed_at.date() > asof
        )
        if newer_prices:
            raise _MarketPriceDraftError(
                f"--asof would roll back current holding market prices: {', '.join(newer_prices)}"
            )
        rows = _read_exact_raw_closes(resolved_sqlite, tickers=tickers, asof=asof)
        observed_at = datetime.combine(asof, time(15, 30), tzinfo=JST)
        prices = tuple(
            MarketPrice(
                ticker=ticker,
                price_yen=Decimal(str(close)),
                observed_at=observed_at.isoformat(),
                source_kind="licensed_dataset",
                price_basis="unadjusted_close",
                source_ref=(f"{sqlite_ref}:jquants_daily_bars:{ticker}:{asof.isoformat()}"),
            )
            for ticker, close, _adjustment_close, _adjustment_factor in rows
        )
        payload = document.model_dump(mode="json")
        payload["as_of"] = max(document.as_of, observed_at).isoformat()
        payload["market_prices"] = [price.model_dump(mode="json") for price in prices]
        draft = PortfolioLedgerDocument.model_validate(payload)
        reconcile_portfolio(draft)
        if db_path is None:
            _write_yaml_exclusive(output_path, draft.model_dump(mode="json"))
        else:
            from baibai_engine.position.drafts import LedgerDraft

            assert expected_head is not None
            write_draft(
                output_path,
                LedgerDraft(
                    kind="market-price",
                    expected_head=expected_head,
                    source=document,
                    replacement=draft,
                    confirmation_required=True,
                ),
            )
        draft_sha256 = _sha256(output_path)
    except (OSError, PortfolioLedgerError, sqlite3.Error, ValueError) as error:
        print(f"error: failed to build market price ledger draft: {error}", file=sys.stderr)
        return 2

    yaml.safe_dump(
        {
            "source_ledger": None if ledger_path is None else str(ledger_path),
            "source_ledger_sha256": source_sha256,
            "market_data_fingerprint": _exact_raw_close_fingerprint(rows, asof=asof),
            "draft_sha256": draft_sha256,
            "output": str(output_path),
        },
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
    )
    return 0


def _repository_source_ref(root: Path, sqlite_path: Path) -> str:
    resolved_root = root.resolve()
    try:
        return sqlite_path.resolve().relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise _MarketPriceDraftError(
            "--sqlite must be below --root for a stable source_ref"
        ) from error


def _read_exact_raw_closes(
    sqlite_path: Path,
    *,
    tickers: tuple[str, ...],
    asof: date,
) -> list[tuple[str, float, float | None, float | None]]:
    if not sqlite_path.is_file():
        raise _MarketPriceDraftError(f"market SQLite not found: {sqlite_path}")
    conn = connect_current(sqlite_path)
    if conn is None:
        raise _MarketPriceDraftError("market SQLite schema is missing or incompatible")
    try:
        conn.execute("BEGIN")
        if not range_covered(conn, "jquants_market_calendar", asof, asof):
            raise _MarketPriceDraftError("market calendar coverage is incomplete for --asof")
        calendar_row = conn.execute(
            "SELECT is_business_day FROM jquants_market_calendar WHERE day = ?",
            (asof.isoformat(),),
        ).fetchone()
        if calendar_row is None or int(calendar_row[0]) != 1:
            raise _MarketPriceDraftError("--asof is not a covered business day")
        if not daily_bars_covered_by_data(conn, asof, asof):
            raise _MarketPriceDraftError("daily-bar coverage is incomplete for --asof")
        fetched = [
            row
            for ticker in tickers
            if (
                row := conn.execute(
                    "SELECT ticker, traded_at, close, adjustment_close, adjustment_factor "
                    "FROM jquants_daily_bars WHERE ticker = ? AND traded_at = ?",
                    (ticker, asof.isoformat()),
                ).fetchone()
            )
            is not None
        ]
    finally:
        conn.close()

    by_ticker = {str(row[0]): row for row in fetched}
    missing = [ticker for ticker in tickers if ticker not in by_ticker]
    if missing:
        raise _MarketPriceDraftError(
            f"raw close is missing for open holdings on {asof.isoformat()}: {', '.join(missing)}"
        )
    rows: list[tuple[str, float, float | None, float | None]] = []
    for ticker in tickers:
        _row_ticker, traded_at, raw_close, adjustment_close, adjustment_factor = by_ticker[ticker]
        if str(traded_at) != asof.isoformat():
            raise _MarketPriceDraftError(f"daily-bar date mismatch for {ticker}")
        close = optional_float(raw_close)
        if close is None or close <= 0:
            raise _MarketPriceDraftError(
                f"raw close is missing or invalid for {ticker} on {asof.isoformat()}"
            )
        rows.append(
            (
                ticker,
                close,
                optional_float(adjustment_close),
                optional_float(adjustment_factor),
            )
        )
    return rows


def _exact_raw_close_fingerprint(
    rows: list[tuple[str, float, float | None, float | None]],
    *,
    asof: date,
) -> str:
    dated_rows = [
        (ticker, asof.isoformat(), close, adjustment_close, adjustment_factor)
        for ticker, close, adjustment_close, adjustment_factor in rows
    ]
    encoded = json.dumps(dated_rows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


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
