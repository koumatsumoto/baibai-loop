"""保存TAの五つの研究入口を読み出す。日次政策・採否・売買を変更しない。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    MODEL_IDS,
    RESEARCH_POLICY_ID,
)
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_discovery as evaluate_technical_discovery,
)
from baibai_engine.storage.filesystem import write_text_new

POLICY_ID = RESEARCH_POLICY_ID


def inspect_technical_discovery(*, generation_id: str, runs_path: Path) -> dict[str, JsonValue]:
    """Read every planned stock, including missing model rows; never substitute latest."""
    with stream_generation_subjects(generation_id, subjects=(), path=runs_path) as header:
        if header is None:
            raise ValueError("unknown model generation")
        metadata = header[0]
    if metadata.subject_plan is None:
        raise ValueError("technical discovery requires a saved stock plan")
    subjects = tuple(
        sorted(subject for subject, plan in metadata.subject_plan.items() if plan.scope == "stock")
    )
    rows = []
    with stream_generation_subjects(
        generation_id, subjects=subjects, model_ids=MODEL_IDS, path=runs_path
    ) as saved:
        if saved is None:
            raise ValueError("unknown model generation")
        metadata, stream = saved
        current = next(stream, None)
        for subject in subjects:
            models = () if current is None or current[0] != subject else current[1]
            rows.append(evaluate_technical_discovery(subject, models=models, metadata=metadata))
            if current is not None and current[0] == subject:
                current = next(stream, None)
    counts: Counter[str] = Counter({"stocks": 0, "matched_any_route": 0})
    for row in rows:
        counts["stocks"] += 1
        counts["matched_any_route"] += bool(row["matched_routes"])
        routes = row["routes"]
        assert isinstance(routes, dict)
        for name, route in routes.items():
            assert isinstance(route, dict)
            counts[f"{name}.{route.get('state')}"] += 1
    return {
        "kind": "technical_discovery_research_diagnostic",
        "policy_id": POLICY_ID,
        "generation_id": generation_id,
        "as_of": metadata.as_of.isoformat(),
        "evaluation_time": metadata.evaluation_time.isoformat(),
        "summary": dict(sorted(counts.items())),
        "rows": list(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--runs-path", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="new private JSON file; never overwrite")
    args = parser.parse_args(argv)
    try:
        if args.out is not None and (args.out.exists() or args.out.is_symlink()):
            raise FileExistsError("output already exists")
        result = inspect_technical_discovery(
            generation_id=args.generation_id, runs_path=args.runs_path
        )
        content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.out is None:
            sys.stdout.write(content)
        else:
            write_text_new(args.out, content)
        return 0
    except (EOFError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"error: technical discovery failed ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
