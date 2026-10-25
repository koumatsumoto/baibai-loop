"""Read-only CLI commands over cached data, saved runs and candidates."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.foundation.date_utils import parse_iso_date as _parse_iso_date
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.machine_analysis.market_context.snapshot import build_market_snapshot
from baibai_engine.machine_analysis.screening.discovery.projection import candidate_rows
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader
from baibai_engine.machine_analysis.screening.screening_run_serializer import (
    published_run_payload,
    serialize_published_run_yaml,
)
from baibai_engine.market.jpx_models import JPXProviderError
from baibai_engine.market.price_reader import latest_daily_bar_date
from baibai_engine.read_api.ticker_profile import build_ticker_profile
from baibai_engine.storage.application.paths import database_path


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _query_asof(asof: str | None, sqlite_path: Path) -> date | None:
    if asof is not None:
        return _parse_iso_date(asof)
    today = datetime.now(JST).date()
    resolved = latest_daily_bar_date(sqlite_path, today - timedelta(days=30), today)
    if resolved is None:
        print(f"no cached daily bars found to resolve --asof: {sqlite_path}", file=sys.stderr)
    return resolved


def ticker_profile_command(
    *,
    ticker: str,
    asof: str | None,
    sqlite_path: Path,
    runs_db_path: Path,
    app_db_path: Path,
    run_revision_id: str | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        normalized = normalize_ticker(ticker)
    except (ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    asof_date = _query_asof(asof, sqlite_path)
    if asof_date is None:
        return 1
    try:
        payload = build_ticker_profile(
            sqlite_path=sqlite_path,
            ticker=normalized,
            asof_date=asof_date,
            runs_db_path=runs_db_path,
            app_db_path=database_path(app_db_path),
            run_revision_id=run_revision_id,
        )
    except (ValueError, sqlite3.Error, JPXProviderError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def market_snapshot_command(
    *,
    asof: str | None,
    weeks: int,
    sqlite_path: Path,
    stdout: TextIO | None = None,
    output_format: str = "yaml",
) -> int:
    out = stdout if stdout is not None else sys.stdout
    if weeks < 1:
        print("--weeks must be greater than zero", file=sys.stderr)
        return 1
    asof_date = _query_asof(asof, sqlite_path)
    if asof_date is None:
        return 1
    payload = build_market_snapshot(
        sqlite_path=sqlite_path,
        asof_date=asof_date,
        history_weeks=weeks,
    )
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=out)
    else:
        yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


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
        run = ScreeningRunReader(runs_db_path).get_run(run_revision_id)
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
