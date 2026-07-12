"""Command-line entry for repository portfolio records.

Composes ledger, holding-review, and outcome inputs. This entry point owns
SQLite composition while position core modules remain free of screening
dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from baibai_loop.market.bars import JQuantsDailyBar
from baibai_loop.market.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_loop.market.jpx_total_return import (
    BenchmarkObservation,
    BenchmarkObservationError,
    load_benchmark_observation,
)
from baibai_loop.market.store import read_daily_bars_for_tickers, read_market_calendar
from baibai_loop.position.holding_review import (
    HoldingReviewError,
    evaluate_holding_review,
    load_holding_review,
    result_to_payload,
    validate_holding_review_sources,
)
from baibai_loop.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerError,
    ReservationEvent,
    load_portfolio_ledger,
    load_portfolio_ledger_with_sha256,
    reconcile_portfolio,
    snapshot_to_payload,
)
from baibai_loop.position.outcome import compute_portfolio_outcome, outcome_to_payload
from baibai_loop.position.result_recording import ResultRecordingError, record_result
from baibai_loop.thesis.holding_review_builder import (
    build_holding_review,
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


def _decimal_argument(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a decimal number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-position")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ledger_parser = subparsers.add_parser(
        "ledger",
        description="Reconcile the repository-only portfolio ledger and emit a YAML snapshot.",
        help="reconcile available cash, reservations, holdings, income, costs, and warnings",
    )
    ledger_parser.add_argument("--root", type=Path, default=Path.cwd())
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
        required=True,
        help="holding review draft YAML path",
    )
    holding_review_parser.add_argument("--root", type=Path, default=Path.cwd())
    holding_build_parser = subparsers.add_parser(
        "holding-review-build",
        help="build a holding review draft from a ready packet, its review, and the ledger",
    )
    holding_build_parser.add_argument("--root", type=Path, default=Path.cwd())
    holding_build_parser.add_argument("--packet", type=Path, required=True)
    holding_build_parser.add_argument("--ledger", type=Path, required=True)
    holding_build_parser.add_argument("--position-id", required=True)
    holding_build_parser.add_argument("--candidate-packet", type=Path)
    holding_build_parser.add_argument("--out", type=Path, required=True)
    result_parser = subparsers.add_parser(
        "record-result",
        help="turn a human-reported open/filled/cancelled result into a ledger draft",
    )
    result_parser.add_argument("--root", type=Path, default=Path.cwd())
    result_parser.add_argument("--ledger", type=Path, required=True)
    result_parser.add_argument("--proposal-ref", required=True)
    result_parser.add_argument("--status", choices=("open", "filled", "cancelled"), required=True)
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
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ledger":
        ledger_path = args.ledger if args.ledger.is_absolute() else args.root / args.ledger
        return _run_ledger(ledger_path)
    if args.command == "holding-review":
        input_path = args.input if args.input.is_absolute() else args.root / args.input
        return _run_holding_review(input_path, root=args.root)
    if args.command == "holding-review-build":
        return _run_holding_review_build(
            root=args.root,
            packet=args.packet,
            ledger=args.ledger,
            position_id=args.position_id,
            candidate_packet=args.candidate_packet,
            out=args.out,
        )
    if args.command == "record-result":
        return _run_record_result(args, now=now)
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
        return _run_outcome(ledger_path, benchmark_path, sqlite_path, out_path)
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_ledger(path: Path) -> int:
    try:
        document = load_portfolio_ledger(path)
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
    ledger_path: Path, benchmark_path: Path, sqlite_path: Path, out_path: Path | None
) -> int:
    """Emit a source-bound TWR result without fetching market data at runtime."""

    try:
        benchmark = load_benchmark_observation(benchmark_path)
    except BenchmarkObservationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not ledger_path.is_file():
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
        ledger = load_portfolio_ledger(ledger_path)
    except PortfolioLedgerError as error:
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
        benchmark_observation_ref=str(benchmark_path),
        benchmark_observation_sha256=_sha256(benchmark_path),
        ledger_ref=str(ledger_path),
        ledger_sha256=_sha256(ledger_path),
        market_data_ref=str(sqlite_path),
        market_data_sha256=_sha256(sqlite_path) if sqlite_path.is_file() else None,
        market_data_coverage_start_date=benchmark.period_start_date.isoformat(),
        market_data_coverage_end_date=benchmark.period_end_date.isoformat(),
        market_data_fingerprint=market_fingerprint,
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


def _run_record_result(args: argparse.Namespace, *, now: datetime | None) -> int:
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
