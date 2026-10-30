"""保存済みモデル世代に現行候補政策を適用し、runs作業コピーへ発行する。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

from baibai_engine.machine_analysis.generation.query import read_generation_metadata
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    select_saved_economic_generation,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.paths import RUNS_DB_PATH


def publish_saved_selection(generation_id: str, *, runs: Path) -> dict[str, object]:
    metadata = read_generation_metadata(generation_id, path=runs)
    if metadata is None:
        raise ValueError("model generation not found")
    if metadata.subject_plan is None:
        raise ValueError("saved generation has no fixed stock subject plan")
    stocks = tuple(
        subject
        for subject, definition in metadata.subject_plan.items()
        if definition.scope == "stock"
    )
    if not stocks:
        raise ValueError("saved generation has no stock subjects")
    policy = EconomicPathPolicy()
    selection = select_saved_economic_generation(
        generation_id, tickers=stocks, policy=policy, path=runs
    )
    selection_id = f"selection-{generation_id}-{policy.method}"
    created = ScreeningSelectionStore(runs).publish(selection_id, selection)
    result = cast(EconomicPathSelection, selection.result)
    return {
        "selection_id": selection_id,
        "generation_id": generation_id,
        "policy": policy.model_dump(mode="json"),
        "stock_count": len(stocks),
        "eligible_count": result.eligible_count,
        "candidate_count": len(result.cards),
        "created": created,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs screening-selection", description=__doc__)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--runs", type=Path, default=RUNS_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = publish_saved_selection(args.generation_id, runs=args.runs)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"screening selection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
