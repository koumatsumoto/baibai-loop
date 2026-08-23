"""CLI for opportunity authoring: prepare / status / thesis-scaffold /
review-scaffold / promote / plan-limit / assessment-scaffold / assessment-publish.

This is also the `baibai-engine research` help surface, so the read-only `evaluate`
command is declared here and forwarded to :mod:`decision_cli`, which keeps its own
arguments and its own exit codes.

Machine output is YAML on stdout only; human explanation and errors go to stderr.
Exit codes:

- 0 success (``defer`` and "no actionable bargain" are normal judgments)
- 2 argparse / CLI usage error
- 3 missing source / checklist / schema / hash makes the request unprocessable
- 4 output collision, input hash drift, or path-confinement violation
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load

from .assessment import (
    AssessmentError,
    BargainAssessment,
    BargainAssessmentService,
    assessment_draft_sha256,
)
from .assessment_scaffold import scaffold_assessment
from .opportunity import (
    OpportunityError,
    compute_status,
    plan_limit,
    prepare_holding_workspace,
    prepare_workspace,
    promote,
    scaffold_review,
    scaffold_thesis,
)
from .thesis import ThesisError


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

    prepare_parser = subparsers.add_parser(
        "prepare", help="build the opportunity workspace from a selection output and ledger"
    )
    prepare_parser.add_argument("--asof", required=True, help="workspace as-of date (YYYY-MM-DD)")
    prepare_parser.add_argument("--selection-output", required=True, type=Path)
    prepare_parser.add_argument("--db", type=Path)
    prepare_parser.add_argument("--workspace", required=True, type=Path)
    prepare_parser.add_argument(
        "--force", action="store_true", help="rebuild an existing local workspace"
    )

    holding_prepare_parser = subparsers.add_parser(
        "holding-prepare", help="build a one-ticker workspace for an open holding"
    )
    holding_prepare_parser.add_argument(
        "--asof", required=True, help="workspace as-of date (YYYY-MM-DD)"
    )
    holding_prepare_parser.add_argument("--db", type=Path)
    holding_prepare_parser.add_argument("--ticker", required=True)
    holding_prepare_parser.add_argument("--workspace", required=True, type=Path)
    holding_prepare_parser.add_argument(
        "--force", action="store_true", help="rebuild an existing local workspace"
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
        help="the session the limit is planned for (YYYY-MM-DD); the close is the "
        "latest complete business day strictly before it",
    )
    thesis_parser.add_argument("--force", action="store_true")

    review_parser = subparsers.add_parser(
        "review-scaffold", help="scaffold an independent review draft bound to the thesis hash"
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

    promote_parser = subparsers.add_parser(
        "promote", help="publish the canonical thesis/review to the application DB"
    )
    promote_parser.add_argument("--workspace", required=True, type=Path)
    promote_parser.add_argument("--ticker", required=True)
    promote_parser.add_argument("--db", type=Path)
    promote_parser.add_argument("--thesis-id")
    promote_parser.add_argument("--supersedes-id")

    plan_parser = subparsers.add_parser(
        "plan-limit", help="derive a planning-only limit/defer from the previous-day raw close"
    )
    plan_parser.add_argument("--thesis", required=True, type=Path)
    plan_parser.add_argument("--db", type=Path)
    plan_parser.add_argument("--sqlite-path", required=True, type=Path)
    plan_parser.add_argument("--target-session", required=True)
    plan_parser.add_argument("--budget-min-yen", type=int, default=200000)
    plan_parser.add_argument("--budget-max-yen", type=int, default=300000)
    plan_parser.add_argument("--output", type=Path)

    assessment_scaffold_parser = subparsers.add_parser(
        "assessment-scaffold",
        help="scaffold a bargain-assessment draft from promoted theses and a proposal",
    )
    assessment_scaffold_parser.add_argument("--db", type=Path)
    assessment_scaffold_parser.add_argument("--assessment-id", required=True)
    assessment_scaffold_parser.add_argument("--asof", required=True)
    assessment_scaffold_parser.add_argument("--shortlist-id", required=True)
    assessment_scaffold_parser.add_argument(
        "--thesis-id",
        action="append",
        required=True,
        help="one promoted thesis per researched ticker; repeat the flag",
    )
    assessment_scaffold_parser.add_argument("--proposal-id")
    assessment_scaffold_parser.add_argument("--out", type=Path)

    assessment_publish_parser = subparsers.add_parser(
        "assessment-publish",
        help="publish the bargain assessment to the application DB",
    )
    assessment_publish_parser.add_argument("draft", type=Path)
    assessment_publish_parser.add_argument("--db", type=Path)
    assessment_publish_parser.add_argument(
        "--check",
        action="store_true",
        help="verify the contract and the machine-value bindings without writing",
    )

    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "evaluate":
        # Returned unwrapped: evaluation reports "not decision-ready" as exit 2,
        # which is not this CLI's usage/data/conflict code set.
        from .decision_cli import main as evaluate_main

        return evaluate_main(arguments[1:], now=now)
    parser = build_parser()
    args = parser.parse_args(arguments)
    out = sys.stdout
    resolved_now = now or datetime.now(JST)
    try:
        match args.command:
            case "prepare":
                prepared = prepare_workspace(
                    asof=_parse_date(args.asof),
                    selection_output=args.selection_output,
                    db_path=args.db,
                    workspace=args.workspace,
                    force=args.force,
                )
                _emit(
                    {
                        "workspace": str(prepared.workspace),
                        "actionable": prepared.actionable,
                        "longlist_size": prepared.longlist_size,
                        "shortlist_slots": prepared.shortlist_slots,
                        "note": None if prepared.actionable else "no actionable bargain",
                    },
                    out,
                )
            case "holding-prepare":
                prepared = prepare_holding_workspace(
                    asof=_parse_date(args.asof),
                    db_path=args.db,
                    ticker=args.ticker,
                    workspace=args.workspace,
                    force=args.force,
                )
                _emit(
                    {
                        "workspace": str(prepared.workspace),
                        "actionable": prepared.actionable,
                        "longlist_size": prepared.longlist_size,
                        "shortlist_slots": prepared.shortlist_slots,
                    },
                    out,
                )
            case "status":
                _emit(compute_status(args.workspace, db_path=args.db), out)
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
                    thesis=args.thesis,
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
            case "assessment-scaffold":
                draft = scaffold_assessment(
                    db_path=args.db,
                    assessment_id=args.assessment_id,
                    as_of=_parse_date(args.asof),
                    shortlist_id=args.shortlist_id,
                    thesis_ids=list(args.thesis_id),
                    proposal_id=args.proposal_id,
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
            case "assessment-publish":
                assessment = BargainAssessment.model_validate(
                    safe_load(args.draft.read_text(encoding="utf-8"))
                )
                service = BargainAssessmentService(args.db)
                if args.check:
                    service.check(assessment)
                    expected = assessment_draft_sha256(assessment)
                    _emit(
                        {
                            "assessment_id": assessment.assessment_id,
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
    except (AssessmentError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except OpportunityError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except ThesisError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
