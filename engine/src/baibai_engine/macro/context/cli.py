"""Macro-context writer and query CLI."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.db import (
    DEFAULT_DB_PATH as DEFAULT_INDICATORS_DB_PATH,
)
from baibai_engine.macro.indicators.db import (
    IndicatorsSchemaError,
)
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH

from .models import (
    MACRO_CONTEXT_STALE_DAYS,
    MacroContextDocument,
    require_integrated_strategy,
    require_registry_agreement,
)
from .scorecard import (
    ScorecardEvaluation,
    ScorecardEvaluationError,
    evaluate_scorecard_from_stores,
)
from .service import (
    MacroContextConflictError,
    MacroContextNotFoundError,
    MacroContextService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine macro context")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser(
        "publish",
        help="publish a macro context report as the new immutable head",
    )
    publish.add_argument("draft", type=Path)
    publish.add_argument("--expected-head")
    publish.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate the draft against the document contract and publication gates "
            "without touching the store (compare-and-swap still runs only on real publish)"
        ),
    )
    show = commands.add_parser(
        "show",
        help="print one published report, by id or as the latest one",
    )
    selection = show.add_mutually_exclusive_group(required=True)
    selection.add_argument("--latest", action="store_true")
    selection.add_argument("--context-id")
    show.add_argument("--asof", required=True, type=date.fromisoformat)
    show.add_argument("--format", choices=("yaml", "json"), default="yaml")
    commands.add_parser(
        "head",
        help="print the current head id, for the compare-and-swap on the next publish",
    )
    monitor = commands.add_parser(
        "monitor",
        help="measure whether the existing consumer freshness policy requests human review",
    )
    monitor.add_argument("--asof", required=True, type=date.fromisoformat)
    monitor.add_argument("--format", choices=("yaml", "json"), default="yaml")
    scorecard = commands.add_parser(
        "scorecard",
        help="score a report's machine-checkable claims against the indicator store",
    )
    scorecard.add_argument("--context-id", required=True)
    scorecard.add_argument("--asof", required=True, type=date.fromisoformat)
    scorecard.add_argument(
        "--indicators-db",
        type=Path,
        default=DEFAULT_INDICATORS_DB_PATH,
    )
    scorecard.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    scorecard.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = MacroContextService(args.db)
    try:
        if args.command == "publish":
            raw = safe_load(args.draft.read_text(encoding="utf-8"))
            document = MacroContextDocument.model_validate(raw)
            if args.check:
                if args.expected_head is not None:
                    raise ValueError(
                        "--check validates without touching the store; drop --expected-head"
                    )
                require_integrated_strategy(document)
                require_registry_agreement(document)
                _emit({"check": "ok", "context_id": document.context_id})
            else:
                _emit(service.publish(document, expected_head=args.expected_head).payload())
        elif args.command == "show":
            if args.latest:
                latest_document = service.latest_for(args.asof)
                _emit(
                    None if latest_document is None else latest_document.payload(),
                    output_format=args.format,
                )
            else:
                _emit(
                    service.get_for(args.context_id, as_of=args.asof).payload(),
                    output_format=args.format,
                )
        elif args.command == "head":
            _emit({"context_id": service.head_id()})
        elif args.command == "monitor":
            latest_document = service.latest_for(args.asof)
            age_days = None if latest_document is None else (args.asof - latest_document.as_of).days
            stale = age_days is not None and age_days > MACRO_CONTEXT_STALE_DAYS
            _emit(
                {
                    "status": "no_ai",
                    "reason": (
                        "missing_context"
                        if age_days is None
                        else (
                            "consumer_freshness_exceeded" if stale else "consumer_freshness_current"
                        )
                    ),
                    "asof": args.asof.isoformat(),
                    "context_id": (None if latest_document is None else latest_document.context_id),
                    "context_asof": (
                        None if latest_document is None else latest_document.as_of.isoformat()
                    ),
                    "age_days": age_days,
                    "stale_after_days": MACRO_CONTEXT_STALE_DAYS,
                    "consumer_stale": stale,
                    "human_action_recommended": age_days is None or stale,
                },
                output_format=args.format,
            )
        elif args.command == "scorecard":
            evaluation = evaluate_scorecard_from_stores(
                context_db=args.db,
                indicators_db_path=args.indicators_db,
                context_id=args.context_id,
                asof=args.asof,
                accessed_at=now or datetime.now(JST),
                rules_path=args.rules,
            )
            if args.format == "json":
                print(json.dumps(evaluation.payload(), ensure_ascii=False))
            else:
                _print_scorecard(evaluation)
        else:  # pragma: no cover
            raise AssertionError(f"unreachable macro context command: {args.command}")
    except (
        OSError,
        ValueError,
        ValidationError,
        IndicatorsSchemaError,
        MacroContextConflictError,
        MacroContextNotFoundError,
        ScorecardEvaluationError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _emit(payload: object, *, output_format: str = "yaml") -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


def _print_scorecard(evaluation: ScorecardEvaluation) -> None:
    print(
        "# macro scorecard "
        f"context={evaluation.context_id} asof={evaluation.snapshot_asof.isoformat()}"
    )
    print(f"# vintage_policy={evaluation.vintage_policy}")
    print(f"# rules_revision={evaluation.rules_revision}")
    print(
        "case\tcondition\tstatus\tseries_id\tcomparison\tthreshold\tdeadline\t"
        "evaluated_through\tobserved_at\tvalue"
    )
    for result in evaluation.results:
        observation = result.observation
        print(
            "\t".join(
                (
                    result.case,
                    str(result.condition_index),
                    result.status,
                    result.series_id,
                    result.comparison,
                    f"{result.threshold:g}",
                    result.deadline.isoformat(),
                    result.evaluated_through.isoformat(),
                    "-" if observation is None else observation.observed_at.isoformat(),
                    "-" if observation is None else f"{observation.value:g}",
                )
            )
        )


__all__ = ["build_parser", "main"]
