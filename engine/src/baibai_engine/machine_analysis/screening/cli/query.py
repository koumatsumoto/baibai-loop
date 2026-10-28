"""Read-only CLI commands over cached data, saved runs and candidates."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.storage.filesystem import write_text_atomic
from baibai_engine.storage.yaml import NoAliasSafeDumper as _NoAliasDumper
from baibai_engine.machine_analysis.screening.discovery.projection import candidate_rows
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader
from baibai_engine.machine_analysis.screening.screening_run_serializer import (
    published_run_payload,
    serialize_published_run_yaml,
)
from baibai_engine.storage.sqlite.machine_analysis import run_store_path


def run_show_command(
    *,
    run_revision_id: str,
    runs_db_path: Path | None = None,
    output_path: Path | None = None,
    force: bool = False,
    stdout: TextIO | None = None,
    output_format: str = "yaml",
    candidates_only: bool = False,
) -> int:
    """Read one fixed revision; never publish or recalculate an analysis."""
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1
    out = stdout if stdout is not None else sys.stdout
    try:
        source = run_store_path(runs_db_path)
        if output_path is not None and (
            output_path.resolve() == source.resolve()
            or (output_path.exists() and source.exists() and output_path.samefile(source))
        ):
            raise ValueError("export output must not replace the input run store")
        run = ScreeningRunReader(source).get_run(run_revision_id)
        if run is None:
            raise ValueError(f"unknown run_revision_id: {run_revision_id}")
        payload = published_run_payload(run)
        if candidates_only:
            rows = candidate_rows(run.stock_analyses)
            payload = {
                "run_revision_id": run.run_revision_id,
                "public_run_id": run.public_run_id,
                "as_of": run.as_of_date,
                "run_at": run.run_at,
                "screening_rules_hash": run.payload.get("screening_rules_hash"),
                "candidate_discovery_method": run.payload["candidate_discovery"]["method"],
                "candidates": list(rows),
                "count": len(rows),
                "input_basis": "saved_stock_analyses",
            }
        rendered = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            if output_format == "json"
            else (
                yaml.dump(payload, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
                if candidates_only
                else serialize_published_run_yaml(run)
            )
        )
        if output_path is not None:
            write_text_atomic(output_path, rendered)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"screening run read/export failed: {error}", file=sys.stderr)
        return 1
    out.write(rendered)
    return 0


def candidates_show_command(
    *,
    run_revision_id: str,
    runs_db_path: Path | None = None,
    output_path: Path | None = None,
    force: bool = False,
    stdout: TextIO | None = None,
    output_format: str = "yaml",
) -> int:
    return run_show_command(
        run_revision_id=run_revision_id,
        runs_db_path=runs_db_path,
        output_path=output_path,
        force=force,
        stdout=stdout,
        output_format=output_format,
        candidates_only=True,
    )
