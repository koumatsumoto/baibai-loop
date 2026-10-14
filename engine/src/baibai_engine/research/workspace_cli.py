"""CLI for selected Research authoring:
selection-context / prepare / status / thesis-scaffold /
review-scaffold / promote / plan-limit / capital-allocation-scaffold / capital-allocation-publish.

This is also the `baibai-engine research` help surface, so the read-only `evaluate`
command is declared here and forwarded to :mod:`thesis_evaluation_cli`, which keeps its own
arguments and its own exit codes.

Machine output is YAML on stdout, except calculate emits JSON; human explanation
and errors go to stderr. Calculate retains its own arguments and exit codes.
Exit codes:

- 0 success (``defer`` and ``no_allocation`` are normal judgments)
- 2 argparse / CLI usage error
- 3 missing source / schema / hash makes the request unprocessable
- 4 output collision, input hash drift, or path-confinement violation
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

from .capital_allocation import (
    CapitalAllocationAssessment,
    CapitalAllocationError,
    capital_allocation_draft_sha256,
)
from .capital_allocation_scaffold import scaffold_capital_allocation
from .capital_allocation_service import CapitalAllocationAssessmentService
from .operation.service import OperationConflictError
from .planning import plan_limit
from .selection import ResearchRequest, ResearchSelectionDraft
from .selection_query import ResearchInputsRequiredError, load_selection_context
from .thesis import ThesisError
from .workspace import (
    ResearchWorkspaceError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    scaffold_review,
    scaffold_thesis,
)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise SystemExit(f"invalid ISO date: {raw}") from error


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

    thesis_parser = subparsers.add_parser(
        "thesis-scaffold", help="scaffold a thesis draft with the previous-day raw close"
    )
    thesis_parser.add_argument("--workspace", required=True, type=Path)
    thesis_parser.add_argument("--db", type=Path)
    thesis_parser.add_argument("--ticker", required=True)
    thesis_parser.add_argument("--sqlite-path", required=True, type=Path)
    thesis_parser.add_argument(
        "--target-session",
        required=True,
        help="formal enterprise valuation date (YYYY-MM-DD); preserve source and quote dates",
    )
    thesis_parser.add_argument("--force", action="store_true")
    thesis_parser.add_argument("--from-thesis-id")

    review_parser = subparsers.add_parser(
        "review-scaffold",
        help="bind a Thesis Review draft after the thesis content is stable",
        description="Bind a Thesis Review draft after the thesis content is stable.",
    )
    review_parser.add_argument("--workspace", required=True, type=Path)
    review_parser.add_argument("--db", type=Path)
    review_parser.add_argument("--ticker", required=True)
    review_parser.add_argument("--force", action="store_true")

    # Listed for discovery; `main` hands this off before parsing so the evaluation
    # command keeps its own arguments, its own `--help`, and its own exit codes.
    subparsers.add_parser(
        "evaluate",
        help="evaluate a thesis draft against the decision gate (read-only)",
        add_help=False,
    )
    subparsers.add_parser(
        "calculate",
        help="calculate draft numbers from typed inputs (JSON, no writes)",
        add_help=False,
    )

    promote_parser = subparsers.add_parser(
        "promote", help="publish the canonical thesis/review to the application DB"
    )
    promote_parser.add_argument("--workspace", required=True, type=Path)
    promote_parser.add_argument("--ticker", required=True)
    promote_parser.add_argument("--db", type=Path)
    promote_parser.add_argument("--thesis-id")
    promote_parser.add_argument("--supersedes-id")

    plan_parser = subparsers.add_parser(
        "plan-limit",
        help="derive a planning-only limit/defer from current capital and raw close",
    )
    plan_parser.add_argument("--capital-allocation-assessment-id", required=True)
    plan_parser.add_argument("--db", type=Path)
    plan_parser.add_argument("--sqlite-path", required=True, type=Path)
    plan_parser.add_argument(
        "--target-session",
        required=True,
        help="next unexpired trading session in the market calendar (YYYY-MM-DD)",
    )
    plan_parser.add_argument("--budget-min-yen", type=int, default=200000)
    plan_parser.add_argument("--budget-max-yen", type=int, default=300000)
    plan_parser.add_argument("--output", type=Path)

    assessment_scaffold_parser = subparsers.add_parser(
        "capital-allocation-scaffold",
        help="scaffold a capital-allocation-assessment draft from promoted theses",
    )
    assessment_scaffold_parser.add_argument("--db", type=Path)
    assessment_scaffold_parser.add_argument("--capital-allocation-assessment-id", required=True)
    assessment_scaffold_parser.add_argument("--asof", required=True)
    assessment_scaffold_parser.add_argument("--operation-id", required=True)
    assessment_scaffold_parser.add_argument("--out", type=Path)

    assessment_publish_parser = subparsers.add_parser(
        "capital-allocation-publish",
        help="publish the capital-allocation assessment to the application DB",
    )
    assessment_publish_parser.add_argument("draft", type=Path)
    assessment_publish_parser.add_argument("--db", type=Path)
    assessment_publish_parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify contract/machine bindings and print the content-review digest; "
            "review_binding=stale is expected before review"
        ),
    )

    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "calculate":
        from .calculation_cli import main as calculate_main

        return calculate_main(arguments[1:])
    if arguments and arguments[0] == "evaluate":
        # Returned unwrapped: evaluation reports "not decision-ready" as exit 2,
        # which is not this CLI's usage/data/conflict code set.
        from .thesis_evaluation_cli import main as evaluate_main

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
            case "thesis-scaffold":
                _emit(
                    scaffold_thesis(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        sqlite_path=args.sqlite_path,
                        target_session=_parse_date(args.target_session),
                        retrieved_at=resolved_now,
                        db_path=args.db,
                        force=args.force,
                        from_thesis_id=args.from_thesis_id,
                    ),
                    out,
                )
            case "review-scaffold":
                _emit(
                    scaffold_review(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        db_path=args.db,
                        force=args.force,
                    ),
                    out,
                )
            case "promote":
                promoted = promote(
                    workspace=args.workspace,
                    ticker=args.ticker,
                    db_path=args.db,
                    thesis_id=args.thesis_id,
                    supersedes_id=args.supersedes_id,
                    now=resolved_now,
                )
                _emit(
                    {
                        "thesis_id": promoted.thesis_id,
                        "review_id": promoted.review_id,
                        "thesis_sha256": promoted.thesis_sha256,
                    },
                    out,
                )
            case "plan-limit":
                payload = plan_limit(
                    capital_allocation_assessment_id=args.capital_allocation_assessment_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    target_session=_parse_date(args.target_session),
                    budget_min_yen=args.budget_min_yen,
                    budget_max_yen=args.budget_max_yen,
                    now=resolved_now,
                )
                if args.output is not None:
                    from baibai_engine.foundation.filesystem import write_text_atomic

                    write_text_atomic(
                        args.output,
                        yaml.safe_dump(
                            payload, sort_keys=False, allow_unicode=True, default_flow_style=False
                        ),
                    )
                _emit(payload, out)
            case "capital-allocation-scaffold":
                draft = scaffold_capital_allocation(
                    db_path=args.db,
                    capital_allocation_assessment_id=args.capital_allocation_assessment_id,
                    as_of=_parse_date(args.asof),
                    operation_id=args.operation_id,
                    published_at=resolved_now,
                )
                if args.out is not None:
                    from baibai_engine.foundation.filesystem import write_text_atomic

                    write_text_atomic(
                        args.out,
                        yaml.safe_dump(
                            draft, sort_keys=False, allow_unicode=True, default_flow_style=False
                        ),
                    )
                _emit(draft, out)
            case "capital-allocation-publish":
                assessment = CapitalAllocationAssessment.model_validate(
                    safe_load(args.draft.read_text(encoding="utf-8"))
                )
                service = CapitalAllocationAssessmentService(args.db, clock=lambda: resolved_now)
                if args.check:
                    service.check(assessment)
                    expected = capital_allocation_draft_sha256(assessment)
                    _emit(
                        {
                            "capital_allocation_assessment_id": (
                                assessment.capital_allocation_assessment_id
                            ),
                            "check": "pass",
                            "draft_sha256": expected,
                            "review_binding": (
                                "match" if assessment.review.draft_sha256 == expected else "stale"
                            ),
                        },
                        out,
                    )
                else:
                    _emit(service.publish(assessment).payload(), out)
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
    except OperationConflictError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4 if args.command == "prepare" else 3
    except CapitalAllocationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
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
    except ThesisError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
