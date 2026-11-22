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

from baibai_engine.investment_judgment.draft_files import parse_authored_yaml
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalConflictError
from baibai_engine.investment_judgment.workflow.discovery import (
    DISCOVERY_QUESTIONS,
    PriceCoordinateMinimum,
)
from baibai_engine.investment_judgment.workflow.operation.service import OperationConflictError
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    validate_input_known_at,
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
from baibai_engine.storage.filesystem import paths_refer_to_same_file
from baibai_engine.storage.paths import (
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.machine_analysis import run_store_path


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
        "proposal-records", help="proposal corrections and human reports", add_help=False
    )
    subparsers.add_parser(
        "market-context", help="explicit market judgment and saved history", add_help=False
    )
    subparsers.add_parser(
        "company-research", help="save/revisit partial company research", add_help=False
    )
    subparsers.add_parser(
        "judgment-retrospective",
        help="compare a saved judgment with later evidence",
        add_help=False,
    )
    subparsers.add_parser(
        "shared-context", help="discover/read fixed shared model subjects", add_help=False
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
    context_parser.add_argument("--known-at", type=datetime.fromisoformat)
    context_parser.add_argument("--generation-id")
    context_parser.add_argument("--screening-selection-id")
    context_parser.add_argument(
        "--discovery-question",
        choices=tuple(DISCOVERY_QUESTIONS),
        help="inspect a finite page of the fixed universe for one economic question",
    )
    context_parser.add_argument(
        "--reported-pbr-max",
        type=float,
        metavar="MULTIPLE",
        help="apply an explicit PBR ceiling to reported_assets using fixed company financials",
    )
    context_parser.add_argument(
        "--price-coordinate-min",
        nargs=4,
        metavar=("PATH", "VARIANT", "PRICE_GROUP", "VALUE"),
        help="compare an explicit saved price coordinate within the question; no default",
    )
    context_parser.add_argument(
        "--screening-page",
        type=int,
        default=0,
        help="zero-based page of the same discovery question",
    )
    context_parser.add_argument("--db", type=Path)
    context_parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    context_parser.add_argument("--out", type=Path)
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
        default=...,
        help=(
            "display latest N saved growth periods, or all; "
            "defaults to the existing workspace setting, or 12 for a new workspace; "
            "does not change saved inputs"
        ),
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
    seed = assessment_parser.add_mutually_exclusive_group()
    seed.add_argument("--from-assessment-id")
    seed.add_argument("--from-research-id")

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

    context = subparsers.add_parser(
        "trade-proposal-context",
        help="read one fixed authoring snapshot and optionally export a work file",
    )
    context.add_argument("--operation-id", required=True)
    context.add_argument("--proposal-id", required=True)
    context.add_argument("--db", type=Path)
    context.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    context.add_argument("--out", type=Path)
    allocation = subparsers.add_parser(
        "trade-proposal-compare-holdings",
        help="compare explicit gross recovery scenarios without writes",
    )
    allocation.add_argument("assumptions", type=Path)
    source = allocation.add_mutually_exclusive_group(required=True)
    source.add_argument("--draft", type=Path)
    source.add_argument("--proposal-id")
    allocation.add_argument("--db", type=Path, help="application DB for a saved proposal only")
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
    if arguments and arguments[0] == "proposal-records":
        from baibai_engine.cli.proposal_records import main as proposal_records_main

        return proposal_records_main(arguments[1:])
    if arguments and arguments[0] == "company-research":
        from baibai_engine.cli.company_research import main as company_research_main

        return company_research_main(arguments[1:], now=now)
    if arguments and arguments[0] == "judgment-retrospective":
        from baibai_engine.cli.judgment_retrospective import main as judgment_retrospective_main

        return judgment_retrospective_main(arguments[1:], now=now)
    if arguments and arguments[0] == "market-context":
        from baibai_engine.cli.market_assessment import main as market_context_main

        return market_context_main(arguments[1:], now=now)
    if arguments and arguments[0] == "shared-context":
        from baibai_engine.cli.shared_model_context import main as shared_context_main

        return shared_context_main(arguments[1:])
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
                known_at = args.known_at or resolved_now
                validate_input_known_at(known_at, now=resolved_now)
                if args.out is not None and any(
                    paths_refer_to_same_file(args.out, source)
                    for source in (run_store_path(args.runs_path), database_path(args.db))
                ):
                    raise ValueError("selection context export must not replace an input database")
                minimum = None
                if args.price_coordinate_min is not None:
                    path_id, variant, group, raw = args.price_coordinate_min
                    minimum = PriceCoordinateMinimum(path_id, variant, group, float(raw))
                payload = load_generated_selection_context(
                    request=request,
                    research_as_of=args.research_asof,
                    generation_id=args.generation_id,
                    screening_selection_id=args.screening_selection_id,
                    known_at=known_at,
                    runs_path=args.runs_path,
                    db_path=args.db,
                    comparison_tickers=tuple(args.compare_ticker),
                    screening_page=args.screening_page,
                    discovery_question=args.discovery_question,
                    price_coordinate_min=minimum,
                    reported_pbr_max=args.reported_pbr_max,
                )
                content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                if args.out is not None:
                    from baibai_engine.storage.filesystem import write_text_atomic

                    write_text_atomic(args.out, content)
                out.write(content)
                return 3 if payload["status"] == "inputs_required" else 0
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
                        from_research_id=args.from_research_id,
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
                from baibai_engine.investment_judgment.trade_proposal.scaffold import (
                    scaffold_proposal,
                )
                from baibai_engine.storage.filesystem import write_text_new

                if args.out is not None and (args.out.exists() or args.out.is_symlink()):
                    raise ResearchWorkspaceConflictError("proposal output already exists")
                context = build_proposal_context(
                    operation_id=args.operation_id,
                    proposal_id=args.proposal_id,
                    db_path=args.db,
                    sqlite_path=args.sqlite_path,
                    now=resolved_now,
                )
                if args.out is not None:
                    content = yaml.safe_dump(
                        scaffold_proposal(context), sort_keys=False, allow_unicode=True
                    )
                    try:
                        write_text_new(args.out, content)
                    except FileExistsError as error:
                        raise ResearchWorkspaceConflictError(
                            "proposal output already exists"
                        ) from error
                out.write(json.dumps(context.payload(), ensure_ascii=False) + "\n")
            case "trade-proposal-compare-holdings":
                from contextlib import closing

                from baibai_engine.investment_judgment.trade_proposal.contracts import (
                    TradeProposalDraft,
                )
                from baibai_engine.investment_judgment.trade_proposal.holding_allocation import (
                    AllocationAlternatives,
                    compare_holding_allocations,
                )
                from baibai_engine.investment_judgment.trade_proposal.query import (
                    is_erroneous_proposal_as_of,
                    load_investment_proposal,
                )
                from baibai_engine.primitives.json import canonical_json
                from baibai_engine.primitives.yaml import strict_safe_load
                from baibai_engine.storage.sqlite.application.read import connect_read_only

                def read_yaml(path: Path) -> object:
                    try:
                        return strict_safe_load(path.read_text(encoding="utf-8"))
                    except yaml.YAMLError as error:
                        raise ValueError(f"invalid comparison YAML: {path}") from error

                plans = AllocationAlternatives.model_validate(read_yaml(args.assumptions))
                fixed: TradeProposalDraft
                if args.draft is not None:
                    if args.db is not None:
                        raise ValueError("--db is only for a saved --proposal-id")
                    fixed = TradeProposalDraft.model_validate(read_yaml(args.draft))
                    role = "draft_not_verified_publication"
                else:
                    with closing(connect_read_only(args.db)) as connection:
                        connection.execute("BEGIN")
                        fixed = load_investment_proposal(connection, args.proposal_id)
                        if is_erroneous_proposal_as_of(connection, args.proposal_id, resolved_now):
                            raise ValueError(
                                "the selected original has a recorded error correction"
                            )
                    role = "exact_saved_proposal_not_current_terms"
                out.write(
                    canonical_json(
                        {"source_role": role, **compare_holding_allocations(fixed, plans)}
                    )
                    + "\n"
                )
            case "trade-proposal-check" | "trade-proposal-publish":
                from baibai_engine.investment_judgment.trade_proposal.contracts import (
                    load_proposal_draft,
                )
                from baibai_engine.investment_judgment.trade_proposal.service import (
                    TradeProposalService,
                )

                document = args.draft.read_text(encoding="utf-8")
                service = TradeProposalService(
                    args.db,
                    sqlite_path=args.sqlite_path,
                    clock=None if now is None else lambda: resolved_now,
                )
                publishing = args.command == "trade-proposal-publish"
                if publishing:
                    published = service.publish(document)
                    _emit(published.model_dump(mode="json"), out)
                    return 0
                proposal = load_proposal_draft(parse_authored_yaml(document, label="proposal"))
                validation = service.check(proposal, require_review=False)
                expected = validation.draft_sha256
                binding = (
                    "unreviewed"
                    if proposal.review is None
                    else "match"
                    if proposal.review.draft_sha256 == expected
                    else "stale"
                )
                _emit(
                    {
                        **validation.payload(),
                        "proposal_id": proposal.proposal_id,
                        "check_scope": "authored_draft_precheck",
                        "review_binding": binding,
                    },
                    out,
                )
                return 0 if validation.valid else 3
            case "trade-proposal-preview":
                from baibai_engine.investment_judgment.trade_proposal.preview import (
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
