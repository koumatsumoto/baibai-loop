"""固定GenerationのTA探索→財務成長案を順に比較する読取専用診断。

全stock planを分母に残す。通常ScreeningSelection、Research選定、Proposalを
作成・変更せず、候補枠やscoreを追加しない。出力は未採択の実験結果である。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue
from tools.experiments.financial_growth_gate import POLICY_ID as FINANCIAL_POLICY_ID
from tools.experiments.financial_growth_gate import evaluate_financial_growth_gate
from tools.experiments.technical_discovery import POLICY_ID as TECHNICAL_POLICY_ID
from tools.experiments.technical_discovery import inspect_technical_discovery

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.storage.filesystem import write_text_new

_FINANCIAL_MODELS = ("company_financials", "company_growth")


def _technical_state(row: dict[str, JsonValue]) -> Literal["matched", "not_matched", "unavailable"]:
    if row["matched_routes"]:
        return "matched"
    routes = cast(dict[str, dict[str, JsonValue]], row["routes"])
    return (
        "unavailable"
        if any(route["state"] == "unavailable" for route in routes.values())
        else "not_matched"
    )


def inspect_technical_financial_candidates(
    *, generation_id: str, runs_path: Path
) -> dict[str, JsonValue]:
    """Inspect one saved generation, never loading financial models before TA discovery."""
    technical = inspect_technical_discovery(generation_id=generation_id, runs_path=runs_path)
    technical_rows = cast(list[dict[str, JsonValue]], technical["rows"])
    subjects = tuple(
        sorted({cast(str, row["ticker"]) for row in technical_rows if row["matched_routes"]})
    )
    financial: dict[str, dict[str, JsonValue]] = {}
    if subjects:
        with stream_generation_subjects(
            generation_id, subjects=subjects, model_ids=_FINANCIAL_MODELS, path=runs_path
        ) as saved:
            if saved is None:
                raise ValueError("chosen model generation is unavailable")
            metadata, stream = saved
            if (
                metadata.generation_id != technical["generation_id"]
                or metadata.as_of.isoformat() != technical["as_of"]
                or metadata.evaluation_time.isoformat() != technical["evaluation_time"]
            ):
                raise ValueError("financial generation differs from technical discovery")
            current = next(stream, None)
            for subject in subjects:
                models = () if current is None or current[0] != subject else current[1]
                financial[subject] = evaluate_financial_growth_gate(
                    subject, models=models, metadata=metadata
                )
                if current is not None and current[0] == subject:
                    current = next(stream, None)

    rows: list[JsonValue] = []
    technical_counts: Counter[str] = Counter()
    financial_counts: Counter[str] = Counter()
    for item in technical_rows:
        ticker = cast(str, item["ticker"])
        gate = financial.get(
            ticker,
            {"state": "not_evaluated", "reason": "no_matched_technical_route"},
        )
        technical_counts[_technical_state(item)] += 1
        financial_counts[cast(str, gate["state"])] += 1
        rows.append({"ticker": ticker, "technical": item, "financial_gate": gate})
    return {
        "kind": "technical_financial_research_diagnostic",
        "adoption_status": "unadopted_research_only",
        "read_semantics": "saved_generation_not_point_in_time_reconstruction",
        "row_scope": "all_saved_stock_plan_subjects_no_ranking_or_quota",
        "generation_id": technical["generation_id"],
        "as_of": technical["as_of"],
        "evaluation_time": technical["evaluation_time"],
        "policy_ids": {"technical": TECHNICAL_POLICY_ID, "financial": FINANCIAL_POLICY_ID},
        "summary": {
            "planned_stocks": len(technical_rows),
            "technical": {
                state: technical_counts[state]
                for state in ("matched", "not_matched", "unavailable")
            },
            "financial_gate": {
                state: financial_counts[state]
                for state in (
                    "research_candidate",
                    "not_selected",
                    "input_unresolved",
                    "not_evaluated",
                )
            },
        },
        "rows": rows,
        "not_measured": [
            "consumer_receipt_time_or_prospective_enrollment",
            "financial_safety_or_investment_acceptance",
            "l3_research_selection_or_trading",
            "forward_returns_or_policy_effectiveness",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--runs-path", type=Path, required=True)
    parser.add_argument(
        "--out", type=Path, required=True, help="new private JSON file; never overwrite"
    )
    args = parser.parse_args(argv)
    try:
        if args.out.exists() or args.out.is_symlink():
            raise FileExistsError("output already exists")
        result = inspect_technical_financial_candidates(
            generation_id=args.generation_id, runs_path=args.runs_path
        )
        content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        write_text_new(args.out, content)
        return 0
    except (EOFError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(
            f"error: technical-financial inspection failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
