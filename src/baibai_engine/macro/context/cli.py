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

from .models import MacroContextDocument
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
from .triggers import TriggerEvaluation, evaluate_triggers_from_stores


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine macro context")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("draft", type=Path)
    publish.add_argument("--expected-head")
    show = commands.add_parser("show")
    selection = show.add_mutually_exclusive_group(required=True)
    selection.add_argument("--latest", action="store_true")
    selection.add_argument("--context-id")
    show.add_argument("--asof", required=True, type=date.fromisoformat)
    commands.add_parser("head")
    scorecard = commands.add_parser("scorecard")
    scorecard.add_argument("--context-id", required=True)
    scorecard.add_argument("--asof", required=True, type=date.fromisoformat)
    scorecard.add_argument(
        "--indicators-db",
        type=Path,
        default=DEFAULT_INDICATORS_DB_PATH,
    )
    scorecard.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    scorecard.add_argument("--format", choices=("table", "json"), default="table")
    triggers = commands.add_parser(
        "triggers",
        help="check a report's machine-checkable invalidation conditions",
    )
    triggers.add_argument("--context-id", required=True)
    triggers.add_argument("--asof", required=True, type=date.fromisoformat)
    triggers.add_argument(
        "--indicators-db",
        type=Path,
        default=DEFAULT_INDICATORS_DB_PATH,
    )
    triggers.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = MacroContextService(args.db)
    try:
        if args.command == "publish":
            raw = safe_load(args.draft.read_text(encoding="utf-8"))
            document = MacroContextDocument.model_validate(raw)
            _emit(service.publish(document, expected_head=args.expected_head).payload())
        elif args.command == "show":
            if args.latest:
                latest_document = service.latest_for(args.asof)
                _emit(None if latest_document is None else latest_document.payload())
            else:
                _emit(service.get_for(args.context_id, as_of=args.asof).payload())
        elif args.command == "head":
            _emit({"context_id": service.head_id()})
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
        elif args.command == "triggers":
            triggers = evaluate_triggers_from_stores(
                context_db=args.db,
                indicators_db_path=args.indicators_db,
                context_id=args.context_id,
                asof=args.asof,
            )
            if args.format == "json":
                print(json.dumps(triggers.payload(), ensure_ascii=False))
            else:
                _print_triggers(triggers)
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


def _emit(payload: object) -> None:
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
        "settlement_ready_on\tevaluated_through\tobserved_at\tvalue"
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
                    result.settlement_ready_on.isoformat(),
                    result.evaluated_through.isoformat(),
                    "-" if observation is None else observation.observed_at.isoformat(),
                    "-" if observation is None else f"{observation.value:g}",
                )
            )
        )


def _print_triggers(evaluation: TriggerEvaluation) -> None:
    print(
        "# macro context triggers "
        f"context={evaluation.context_id} asof={evaluation.asof.isoformat()}"
    )
    print(f"# window=({evaluation.context_as_of.isoformat()}, {evaluation.asof.isoformat()}]")
    print("point\tcondition\tstatus\tseries_id\tcomparison\tthreshold\tobserved_at\tvalue\tevent")
    for result in evaluation.results:
        observation = result.observation
        print(
            "\t".join(
                (
                    str(result.point_index),
                    str(result.condition_index),
                    result.status,
                    result.series_id,
                    result.comparison,
                    f"{result.threshold:g}",
                    "-" if observation is None else observation.observed_at.isoformat(),
                    "-" if observation is None else f"{observation.value:g}",
                    result.event,
                )
            )
        )
    fired = evaluation.fired
    print(f"# fired={len(fired)} of {len(evaluation.results)}")


__all__ = ["build_parser", "main"]
