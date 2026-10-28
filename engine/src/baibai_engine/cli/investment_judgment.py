"""Investment Judgmentの選定・Stock Assessment・Trade Proposalの公開CLI。

JSON is used for selection, context and current previews; authoring diagnostics
use YAML. Data failures exit 3, workspace drift/output collisions exit 4. The
read-only stock-assessment-evaluate and calculate commands retain their own exit codes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalConflictError
from baibai_engine.investment_judgment.workflow.operation.service import OperationConflictError
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
)
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    scaffold_assessment,
    scaffold_review,
)
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.paths import (
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)


def _emit(payload: object, out: TextIO) -> None:
    yaml.safe_dump(payload, out, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _growth_display_periods(value: str) -> int | None:
    if value == "all":
        return None
    try:
        count = int(value)
        if count > 0:
            return count
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use a positive period count or all")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine investment-judgment")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "market-context", help="explicit market judgment and saved history", add_help=False
    )
    context_parser = subparsers.add_parser(
        "selection-context", help="read saved inputs for AI target selection"
    )
    context_parser.add_argument("--request", required=True)
    context_parser.add_argument(
        "--scope", required=True, choices=("discover_new", "specified_stocks", "review_positions")
    )
    context_parser.add_argument("--ticker", action="append", default=[])
    context_parser.add_argument(
        "--compare-ticker",
        action="append",
        default=[],
        help="read comparison models from the same generation without selecting them",
    )
    context_parser.add_argument("--research-asof", type=date.fromisoformat)
    context_parser.add_argument("--generation-id")
    context_parser.add_argument("--screening-selection-id")
    context_parser.add_argument("--db", type=Path)
    context_parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    context_parser.add_argument("--out", type=Path)
    calibration_parser = subparsers.add_parser(
        "calibration-context",
        help="read validated E[r] history for a fixed Investment Judgment run",
    )
    calibration_parser.add_argument("--operation-id", required=True)
    calibration_parser.add_argument("--db", type=Path)
    calibration_parser.add_argument(
        "--context-path", type=Path, default=ER_LEVEL_CALIBRATION_CONTEXT_PATH
    )
    calibration_parser.add_argument("--asof", type=date.fromisoformat)
    prepare_parser = subparsers.add_parser(
        "prepare", help="save selected Investment Judgment inputs or resume an Operation"
    )
    source_parser = prepare_parser.add_mutually_exclusive_group(required=True)
    source_parser.add_argument("--selection-file", type=Path)
    source_parser.add_argument("--operation-id")
    prepare_parser.add_argument("--db", type=Path)
    prepare_parser.add_argument("--workspace", required=True, type=Path)
    prepare_parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    prepare_parser.add_argument(
        "--company-growth-periods",
        type=_growth_display_periods,
        default=12,
        help="display latest N saved growth periods, or all; does not change saved inputs",
    )
    prepare_parser.add_argument(
        "--force", action="store_true", help="repair generated projections; retain authored drafts"
    )

    status_parser = subparsers.add_parser(
        "status", help="report workspace completion, drift, and the next command"
    )
    status_parser.add_argument("--workspace", required=True, type=Path)
    status_parser.add_argument("--db", type=Path)

    assessment_parser = subparsers.add_parser(
        "stock-assessment-scaffold", help="scaffold a Stock Assessment draft; preserve source dates"
    )
    assessment_parser.add_argument("--workspace", required=True, type=Path)
    assessment_parser.add_argument("--db", type=Path)
    assessment_parser.add_argument("--ticker", required=True)
    assessment_parser.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    assessment_parser.add_argument("--asof", required=True, type=date.fromisoformat)
    assessment_parser.add_argument("--out", type=Path)
    assessment_parser.add_argument("--force", action="store_true")
    assessment_parser.add_argument("--from-assessment-id")

    review_parser = subparsers.add_parser(
        "independent-review-scaffold", help="bind an independent Review to a stable Assessment"
    )
    review_parser.add_argument("--workspace", required=True, type=Path)
    review_parser.add_argument("--db", type=Path)
    review_parser.add_argument("--ticker", required=True)
    review_parser.add_argument("--force", action="store_true")

    subparsers.add_parser(
        "stock-assessment-evaluate",
        help="evaluate a Stock Assessment draft (read-only)",
        add_help=False,
    )
    subparsers.add_parser(
        "calculate",
        help="calculate draft numbers from typed inputs (JSON, no writes)",
        add_help=False,
    )
    promote_parser = subparsers.add_parser(
        "stock-assessment-promote", help="publish the immutable Stock Assessment/Review pair"
    )
    promote_parser.add_argument("--workspace", required=True, type=Path)
    promote_parser.add_argument("--ticker", required=True)
    promote_parser.add_argument("--db", type=Path)

    for command, help_text in (
        (
            "trade-proposal-context",
            "read fixed Investment Judgment scope and current proposal inputs",
        ),
        ("trade-proposal-scaffold", "scaffold a Trade Proposal with a fixed input snapshot"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--operation-id", required=True)
        child.add_argument("--db", type=Path)
        child.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
        if command == "trade-proposal-scaffold":
            child.add_argument("--proposal-id", required=True)
            child.add_argument("--out", required=True, type=Path)
    for command in ("trade-proposal-check", "trade-proposal-publish"):
        child = subparsers.add_parser(command, help="check/publish a Trade Proposal")
        child.add_argument("draft", type=Path)
        child.add_argument("--db", type=Path)
        child.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    preview_parser = subparsers.add_parser(
        "trade-proposal-preview",
        help="read current conditions separately from saved proposal terms",
    )
    preview_parser.add_argument("--proposal-id", required=True)
    preview_parser.add_argument("--db", type=Path)
    preview_parser.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)

    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "market-context":
        from baibai_engine.cli.market_assessment import main as market_context_main

        return market_context_main(arguments[1:], now=now)
    if arguments and arguments[0] == "calculate":
        from baibai_engine.cli.assessment_calculation import main as calculate_main

        return calculate_main(arguments[1:])
    if arguments and arguments[0] == "stock-assessment-evaluate":
        # Returned unwrapped: evaluation reports "not assessment-ready" as exit 2,
        # which is not this CLI's usage/data/conflict code set.
        from baibai_engine.cli.stock_assessment import main as evaluate_main

        return evaluate_main(arguments[1:], now=now)
    parser = build_parser()
    args = parser.parse_args(arguments)
    out = sys.stdout
    resolved_now = now or datetime.now(JST)
    try:
        match args.command:
            case "selection-context":
                try:
                    request = ResearchRequest(
                        text=args.request, scope=args.scope, requested_tickers=tuple(args.ticker)
                    )
                except ValidationError as error:
                    parser.error(str(error))
                payload = load_generated_selection_context(
                    request=request,
                    research_as_of=args.research_asof,
                    generation_id=args.generation_id,
                    screening_selection_id=args.screening_selection_id,
                    known_at=resolved_now,
                    runs_path=args.runs_path,
                    db_path=args.db,
                    comparison_tickers=tuple(args.compare_ticker),
                )
                content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                if args.out is not None:
                    from baibai_engine.storage.filesystem import write_text_atomic

                    write_text_atomic(args.out, content)
                out.write(content)
                return 3 if payload["status"] == "inputs_required" else 0
            case "calibration-context":
                from baibai_engine.investment_judgment.workflow.calibration_context import (
                    research_calibration_context,
                )
                from baibai_engine.investment_judgment.workflow.operation.progress import (
                    selection_binding,
                )
                from baibai_engine.investment_judgment.workflow.operation.service import (
                    OperationService,
                )

                operation = OperationService(args.db).get(args.operation_id)
                selection = selection_binding(operation.payload)
                if selection is None:
                    raise ValueError("Operation has no fixed Research selection")
                payload = research_calibration_context(
                    selection,
                    context_path=args.context_path,
                    as_of=args.asof or resolved_now.astimezone(JST).date(),
                )
                out.write(
                    json.dumps(
                        {"operation_id": operation.operation_id, **payload}, ensure_ascii=False
                    )
                    + "\n"
                )
            case "prepare":
                if args.selection_file is not None:
                    prepared = prepare_selected_workspace(
                        draft=GeneratedResearchSelectionDraft.model_validate_json(
                            args.selection_file.read_text()
                        ),
                        db_path=args.db,
                        workspace=args.workspace,
                        runs_path=args.runs_path,
                        company_growth_periods=args.company_growth_periods,
                        now=resolved_now,
                        force=args.force,
                    )
                else:
                    prepared = resume_selected_workspace(
                        operation_id=args.operation_id,
                        db_path=args.db,
                        workspace=args.workspace,
                        now=resolved_now,
                        force=args.force,
                        company_growth_periods=args.company_growth_periods,
                    )
                result = asdict(prepared)
                result["workspace"] = str(prepared.workspace)
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
            case "status":
                _emit(compute_status(args.workspace, db_path=args.db, now=resolved_now), out)
            case "stock-assessment-scaffold":
                _emit(
                    scaffold_assessment(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        sqlite_path=args.sqlite_path,
                        as_of=args.asof,
                        retrieved_at=resolved_now,
                        db_path=args.db,
                        force=args.force,
                        from_assessment_id=args.from_assessment_id,
                        out=args.out,
                    ),
                    out,
                )
            case "independent-review-scaffold":
                _emit(
                    scaffold_review(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        db_path=args.db,
                        force=args.force,
                    ),
                    out,
                )
            case "stock-assessment-promote":
                _emit(
                    asdict(
                        promote(
                            workspace=args.workspace,
                            ticker=args.ticker,
                            db_path=args.db,
                            now=resolved_now,
                        )
                    ),
                    out,
                )
            case "trade-proposal-context":
                from baibai_engine.investment_judgment.trade_proposal.inputs import (
                    build_proposal_context,
                )

                context = build_proposal_context(
                    operation_id=args.operation_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                out.write(json.dumps(context.payload(), ensure_ascii=False) + "\n")
            case "trade-proposal-scaffold":
                from baibai_engine.investment_judgment.trade_proposal.scaffold import (
                    scaffold_proposal,
                )
                from baibai_engine.storage.filesystem import write_text_atomic

                if args.out.exists():
                    raise ResearchWorkspaceConflictError("proposal output already exists")
                draft = scaffold_proposal(
                    operation_id=args.operation_id,
                    proposal_id=args.proposal_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                write_text_atomic(
                    args.out,
                    yaml.safe_dump(
                        draft,
                        sort_keys=False,
                        allow_unicode=True,
                        default_flow_style=False,
                    ),
                )
                _emit({"proposal_draft": str(args.out), "proposal_id": args.proposal_id}, out)
            case "trade-proposal-check" | "trade-proposal-publish":
                from baibai_engine.investment_judgment.trade_proposal.contracts import (
                    TradeProposal,
                    proposal_draft_sha256,
                )
                from baibai_engine.investment_judgment.trade_proposal.service import (
                    TradeProposalService,
                )

                try:
                    payload = safe_load(args.draft.read_text(encoding="utf-8"))
                except yaml.YAMLError as error:
                    raise ValueError(f"invalid proposal YAML: {args.draft}: {error}") from error
                proposal = TradeProposal.model_validate(payload)
                service = TradeProposalService(
                    args.db,
                    sqlite_path=args.sqlite_path,
                    clock=lambda: resolved_now,
                )
                if args.command == "trade-proposal-check":
                    validation = service.check(proposal, require_review=False)
                    expected = proposal_draft_sha256(proposal)
                    _emit(
                        {
                            **validation.payload(),
                            "proposal_id": proposal.proposal_id,
                            "draft_sha256": expected,
                            "review_binding": "match"
                            if proposal.review.draft_sha256 == expected
                            else "stale",
                        },
                        out,
                    )
                    return 0 if validation.valid else 3
                _emit(service.publish(proposal).model_dump(mode="json"), out)
            case "trade-proposal-preview":
                from baibai_engine.investment_judgment.trade_proposal.query import (
                    preview_current_terms,
                )

                preview = preview_current_terms(
                    proposal_id=args.proposal_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                out.write(json.dumps(preview.payload(), ensure_ascii=False) + "\n")
            case _:  # pragma: no cover - argparse enforces the command set
                raise AssertionError(f"unreachable command: {args.command!r}")
    except (OperationConflictError, ProposalConflictError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4 if args.command == "prepare" else 3
    except ResearchWorkspaceError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        out.write(
            json.dumps(
                {"status": "invalid_source", "operation_id": None, "reason": str(error)},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
