"""保存価格・masterから市場snapshotを読むCLI。候補政策は実行しない。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.primitives.dates import parse_iso_date as _parse_iso_date
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.primitives.time import JST
from baibai_engine.storage.yaml import NoAliasSafeDumper as _NoAliasDumper
from baibai_engine.market.price_reader import latest_daily_bar_date

from baibai_engine.machine_analysis.models.market_context.snapshot import build_market_snapshot


def _query_asof(asof: str | None, sqlite_path: Path) -> date | None:
    if asof is not None:
        return _parse_iso_date(asof)
    today = datetime.now(JST).date()
    resolved = latest_daily_bar_date(sqlite_path, date.min, today)
    if resolved is None:
        print(f"no cached daily bars found to resolve --asof: {sqlite_path}", file=sys.stderr)
    return resolved


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine market-context snapshot")
    parser.add_argument("--asof", help="evaluation date (default: latest cached trading day)")
    parser.add_argument("--weeks", type=int, default=12, help="weekly history points (default: 12)")
    parser.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return market_snapshot_command(
        asof=args.asof, weeks=args.weeks, sqlite_path=args.sqlite_path, output_format=args.format
    )
