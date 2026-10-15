"""Researchの選定・Stock Assessment・Investment Proposalの公開CLI。

JSON is used for selection, context and current previews; authoring diagnostics
use YAML. Data failures exit 3, workspace drift/output collisions exit 4. The
read-only assessment-evaluate and calculate commands retain their own exit codes.
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

from baibai_engine.foundation.repository_layout import (
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load

from .operation.service import OperationConflictError
from .proposal import ProposalConflictError
from .selection import ResearchRequest, ResearchSelectionDraft
from .selection_query import ResearchInputsRequiredError, load_selection_context
from .workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    scaffold_assessment,
    scaffold_review,
)


def _emit(payload: object, out: TextIO) -> None:
    yaml.safe_dump(payload, out, sort_keys=False, allow_unicode=True, default_flow_style=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine research")
    subparsers = parser.add_subparsers(dest="command", required=True)
    context_parser = subparsers.add_parser(
        "selection-context", help="read saved inputs for AI target selection"
    )
    context_parser.add_argument("--request", required=True)
    context_parser.add_argument(
        "--scope", required=True, choices=("discover_new", "specified_stocks", "review_positions")
    )
    context_parser.add_argument("--ticker", action="append", default=[])
    context_parser.add_argument("--research-asof", type=date.fromisoformat)
    context_parser.add_argument("--run-revision-id")
    context_parser.add_argument("--db", type=Path)
    context_parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    context_parser.add_argument("--out", type=Path)
    prepare_parser = subparsers.add_parser(
        "prepare", help="save selected Research inputs or resume an Operation"
    )
    source_parser = prepare_parser.add_mutually_exclusive_group(required=True)
    source_parser.add_argument("--selection-file", type=Path)
    source_parser.add_argument("--operation-id")
    prepare_parser.add_argument("--db", type=Path)
    prepare_parser.add_argument("--workspace", required=True, type=Path)
    prepare_parser.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    prepare_parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    prepare_parser.add_argument("--indicators-path", type=Path, default=INDICATORS_DB_PATH)
    prepare_parser.add_argument(
        "--force", action="store_true", help="repair generated projections; retain authored drafts"
    )

    status_parser = subparsers.add_parser(
        "status", help="report workspace completion, drift, and the next command"
    )
    status_parser.add_argument("--workspace", required=True, type=Path)
    status_parser.add_argument("--db", type=Path)

    assessment_parser = subparsers.add_parser(
        "assessment-scaffold", help="scaffold a Stock Assessment draft; preserve source dates"
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
        "assessment-review-scaffold", help="bind an independent Review to a stable Assessment"
    )
    review_parser.add_argument("--workspace", required=True, type=Path)
    review_parser.add_argument("--db", type=Path)
    review_parser.add_argument("--ticker", required=True)
    review_parser.add_argument("--force", action="store_true")

    subparsers.add_parser(
        "assessment-evaluate", help="evaluate a Stock Assessment draft (read-only)", add_help=False
    )
    subparsers.add_parser(
        "calculate",
        help="calculate draft numbers from typed inputs (JSON, no writes)",
        add_help=False,
    )
    promote_parser = subparsers.add_parser(
        "assessment-promote", help="publish the immutable Stock Assessment/Review pair"
    )
    promote_parser.add_argument("--workspace", required=True, type=Path)
    promote_parser.add_argument("--ticker", required=True)
    promote_parser.add_argument("--db", type=Path)

    for command, help_text in (
        ("proposal-context", "read fixed Research scope and current proposal inputs"),
        ("proposal-scaffold", "scaffold an Investment Proposal with a fixed input snapshot"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--operation-id", required=True)
        child.add_argument("--db", type=Path)
        child.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
        if command == "proposal-scaffold":
            child.add_argument("--proposal-id", required=True)
            child.add_argument("--out", required=True, type=Path)
    for command in ("proposal-check", "proposal-publish"):
        child = subparsers.add_parser(command, help="check/publish an Investment Proposal")
        child.add_argument("draft", type=Path)
        child.add_argument("--db", type=Path)
        child.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    preview_parser = subparsers.add_parser(
        "proposal-preview", help="read current conditions separately from saved proposal terms"
    )
    preview_parser.add_argument("--proposal-id", required=True)
    preview_parser.add_argument("--db", type=Path)
    preview_parser.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)

    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "calculate":
        from .calculation_cli import main as calculate_main

        return calculate_main(arguments[1:])
    if arguments and arguments[0] == "assessment-evaluate":
        # Returned unwrapped: evaluation reports "not assessment-ready" as exit 2,
        # which is not this CLI's usage/data/conflict code set.
        from .assessment_evaluation_cli import main as evaluate_main

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
                payload = load_selection_context(
                    request=request,
                    research_as_of=args.research_asof,
                    run_revision_id=args.run_revision_id,
                    known_at=resolved_now,
                    runs_path=args.runs_path,
                    db_path=args.db,
                )
                content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                if args.out is not None:
                    from baibai_engine.foundation.filesystem import write_text_atomic

                    write_text_atomic(args.out, content)
                out.write(content)
                return 3 if payload["status"] == "inputs_required" else 0
            case "prepare":
                if args.selection_file is not None:
                    prepared = prepare_selected_workspace(
                        draft=ResearchSelectionDraft.model_validate_json(
                            args.selection_file.read_text()
                        ),
                        db_path=args.db,
                        workspace=args.workspace,
                        sqlite_path=args.sqlite_path,
                        runs_path=args.runs_path,
                        indicators_path=args.indicators_path,
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
                    )
                result = asdict(prepared)
                result["workspace"] = str(prepared.workspace)
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
            case "status":
                _emit(compute_status(args.workspace, db_path=args.db, now=resolved_now), out)
            case "assessment-scaffold":
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
            case "assessment-review-scaffold":
                _emit(
                    scaffold_review(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        db_path=args.db,
                        force=args.force,
                    ),
                    out,
                )
            case "assessment-promote":
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
            case "proposal-context":
                from .proposal_inputs import build_proposal_context

                context = build_proposal_context(
                    operation_id=args.operation_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                out.write(json.dumps(context.payload(), ensure_ascii=False) + "\n")
            case "proposal-scaffold":
                from baibai_engine.foundation.filesystem import write_text_atomic

                from .proposal_scaffold import scaffold_proposal

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
            case "proposal-check" | "proposal-publish":
                from .proposal import InvestmentProposal, proposal_draft_sha256
                from .proposal_service import InvestmentProposalService

                proposal = InvestmentProposal.model_validate(
                    safe_load(args.draft.read_text(encoding="utf-8"))
                )
                service = InvestmentProposalService(
                    args.db,
                    sqlite_path=args.sqlite_path,
                    clock=lambda: resolved_now,
                )
                if args.command == "proposal-check":
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
            case "proposal-preview":
                from .proposal_query import preview_current_terms

                preview = preview_current_terms(
                    proposal_id=args.proposal_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                out.write(json.dumps(preview.payload(), ensure_ascii=False) + "\n")
            case _:  # pragma: no cover - argparse enforces the command set
                raise AssertionError(f"unreachable command: {args.command!r}")
    except ResearchInputsRequiredError as error:
        out.write(
            json.dumps(
                {
                    "status": "inputs_required",
                    "operation_id": None,
                    "missing_inputs": [error.public()],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 3
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
