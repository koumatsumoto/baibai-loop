"""Technical Analysisの明示再計算と、固定Generationの小セクション読取。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.machine_analysis.generation.runners.technical_analysis import technical_analysis
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH

_MODELS = ("technical_analysis", "market_context")


def _section(value: str) -> tuple[str, str]:
    model_id, separator, section = value.partition(".")
    if model_id not in _MODELS or not separator or not section.isidentifier():
        raise argparse.ArgumentTypeError(
            "use technical_analysis.SECTION or market_context.SECTION (one saved top-level section)"
        )
    return model_id, section


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine technical-analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser(
        "show", help="calculate SMA20/50/200, BB20, RSI14 and 12-1 momentum without acquisition"
    )
    show.add_argument("--ticker", required=True)
    show.add_argument("--asof", type=date.fromisoformat, required=True)
    show.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    saved = commands.add_parser(
        "saved", help="read selected saved TA/Market sections as JSONL, without recalculation"
    )
    saved.add_argument("--generation-id", required=True)
    subjects = saved.add_mutually_exclusive_group(required=True)
    subjects.add_argument("--ticker", action="append", help="repeat for explicit stocks")
    subjects.add_argument(
        "--all-stocks", action="store_true", help="use the generation's stock plan"
    )
    saved.add_argument(
        "--section", required=True, action="append", type=_section, metavar="MODEL.SECTION"
    )
    saved.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    return parser


def _price_context(model: GeneratedModel) -> dict[str, JsonValue]:
    payload = model.payload
    context: dict[str, JsonValue]
    if model.model_id == "technical_analysis":
        context = {
            key: payload[key]
            for key in ("as_of", "history_end_date", "price_basis", "read_semantics")
            if key in payload
        }
        price_path = payload.get("price_path")
        if isinstance(price_path, dict):
            context["price_path"] = {
                key: value
                for key, value in price_path.items()
                if key not in {"observations", "annotation"}
            }
        elif "price_path" in payload:
            context["price_path"] = price_path
        return context
    context = {key: payload[key] for key in ("master",) if key in payload}
    price = payload.get("price")
    if isinstance(price, dict):
        context["price"] = {
            key: price[key] for key in ("resolved_date", "price_basis") if key in price
        }
    elif "price" in payload:
        context["price"] = price
    return context


def _saved(args: argparse.Namespace) -> None:
    # The immutable generation ID is retained across both reads; do not resolve
    # latest again or load every company's payload merely to discover its scope.
    with stream_generation_subjects(args.generation_id, subjects=(), path=args.runs_path) as header:
        if header is None:
            raise ValueError("unknown model generation")
        metadata = header[0]
    if args.all_stocks:
        if metadata.subject_plan is None:
            raise ValueError("--all-stocks requires a saved subject plan; use --ticker for V1")
        subjects = tuple(
            sorted(
                subject
                for subject, definition in metadata.subject_plan.items()
                if definition.scope == "stock"
            )
        )
    else:
        subjects = tuple(sorted({normalize_ticker(ticker) for ticker in args.ticker}))
    with stream_generation_subjects(
        args.generation_id, subjects=subjects, model_ids=_MODELS, path=args.runs_path
    ) as saved:
        if saved is None:
            raise ValueError("unknown model generation")
        metadata, rows = saved
        current = next(rows, None)
        for subject in subjects:
            models = {}
            if current is not None and current[0] == subject:
                models = {model.model_id: model for model in current[1]}
            definition = (
                None if metadata.subject_plan is None else metadata.subject_plan.get(subject)
            )
            sections = {}
            missing = []
            for model_id, section in args.section:
                model = models.get(model_id)
                name = f"{model_id}.{section}"
                if model is not None and section in model.payload:
                    sections[name] = model.payload[section]
                else:
                    missing.append(name)
            print(
                json.dumps(
                    {
                        "generation_id": metadata.generation_id,
                        "as_of": metadata.as_of.isoformat(),
                        "evaluation_time": metadata.evaluation_time.isoformat(),
                        "subject": subject,
                        "planned_model_ids": None if definition is None else definition.model_ids,
                        "calculation_revisions": {
                            name: model.calculation_revision for name, model in models.items()
                        },
                        "price_context": {
                            name: _price_context(model) for name, model in models.items()
                        },
                        "sections": sections,
                        "missing_models": [name for name in _MODELS if name not in models],
                        "not_recorded_sections": missing,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            if current is not None and current[0] == subject:
                current = next(rows, None)


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "saved":
            _saved(args)
        else:
            result = technical_analysis(args.db, ticker=args.ticker, as_of=args.asof)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (EOFError, ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"error: technical analysis read failed: {error}", file=sys.stderr)
        return 1
    return 0
