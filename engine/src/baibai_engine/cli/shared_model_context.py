"""固定世代の共有材料を有限に探し、明示対象だけ原形で読む。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.storage.paths import RUNS_DB_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine investment-judgment shared-context")
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--runs-path", type=Path, default=RUNS_DB_PATH)
    parser.add_argument("--subject", help="read one exact shared subject from the saved plan")
    parser.add_argument("--after-subject", help="continue the shared subject catalog")
    parser.add_argument("--limit", type=int, default=20, help="catalog page size, from 1 to 100")
    return parser


def _read(args: argparse.Namespace) -> dict[str, object]:
    with stream_generation_subjects(
        args.generation_id,
        subjects=() if args.subject is None else (args.subject,),
        path=args.runs_path,
    ) as saved:
        if saved is None:
            raise ValueError("unknown model generation")
        metadata, rows = saved
        result: dict[str, object] = {
            "generation_id": metadata.generation_id,
            "as_of": metadata.as_of.isoformat(),
            "evaluation_time": metadata.evaluation_time.isoformat(),
        }
        plan = metadata.subject_plan
        if args.subject is None:
            # Catalog discovery never decompresses company or shared payloads.
            subjects = sorted(
                subject
                for subject, definition in (plan or {}).items()
                if definition.scope != "stock"
                and (args.after_subject is None or subject > args.after_subject)
            )
            page = subjects[: args.limit]
            result.update(
                scope_status="not_recorded" if plan is None else "recorded",
                subjects=[
                    {
                        "subject": subject,
                        "definition": (plan or {})[subject].model_dump(mode="json"),
                    }
                    for subject in page
                ],
                next_after_subject=page[-1] if len(subjects) > len(page) else None,
            )
            return result
        if plan is None:
            raise ValueError("shared subject scope was not recorded in this generation")
        definition = plan.get(args.subject)
        if definition is None or definition.scope == "stock":
            raise ValueError("subject must be an exact shared subject in the saved plan")
        models = tuple(model for _, subject_models in rows for model in subject_models)
        generated = {model.model_id for model in models}
        result.update(
            subject=args.subject,
            definition=definition.model_dump(mode="json"),
            models=[model.model_dump(mode="json") for model in models],
            not_generated=[
                model_id for model_id in definition.model_ids if model_id not in generated
            ],
            input_identities={
                name: identity.model_dump(mode="json", exclude={"descriptor"})
                for name, identity in metadata.inputs.items()
            },
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.subject is not None and args.after_subject is not None:
        parser.error("--after-subject is only for catalog discovery")
    try:
        # Serialize before writing stdout so a failed read cannot resemble an empty result.
        content = json.dumps(_read(args), ensure_ascii=False, indent=2, allow_nan=False)
    except (EOFError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"error: shared context read failed: {error}", file=sys.stderr)
        return 3
    print(content)
    return 0
