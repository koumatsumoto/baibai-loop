"""保存価格・masterから市場snapshotを読むCLI。候補政策は実行しない。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    NoCachedDailyBars,
    build_market_snapshot,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.current_reader import (
    CurrentListingSection,
    read_current_jp_master,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    read_local_listing_master,
)
from baibai_engine.primitives.dates import parse_iso_date as _parse_iso_date
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import NoAliasSafeDumper as _NoAliasDumper
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


def market_snapshot_command(
    *,
    asof: str | None,
    weeks: int,
    sqlite_path: Path,
    stdout: TextIO | None = None,
    output_format: str = "yaml",
    current_listing_master: Path | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    if weeks < 1:
        print("--weeks must be greater than zero", file=sys.stderr)
        return 1
    now = datetime.now(JST)
    asof_date = _parse_iso_date(asof) if asof is not None else now.date()
    listing: CurrentListingSection | None = None

    def unconfirmed_master() -> None:
        # The filename and validation details can contain private source material.
        print(
            "warning: current listing master unreadable; breadth and sectors unavailable",
            file=sys.stderr,
        )

    if current_listing_master is not None:
        try:
            listing = read_local_listing_master(current_listing_master).get("JP")
        except (OSError, ValueError):
            unconfirmed_master()

    def read_master(connection: sqlite3.Connection, session: date) -> MasterSnapshot | None:
        try:
            return read_current_jp_master(
                listing, calendar=connection, as_of=session, evaluation_time=now
            )
        except JQuantsProviderError:
            unconfirmed_master()
            return None

    try:
        payload = build_market_snapshot(
            sqlite_path=sqlite_path,
            asof_date=asof_date,
            history_weeks=weeks,
            use_latest_cached_day=asof is None,
            read_current_master=read_master,
        )
    except NoCachedDailyBars:
        print(f"no cached daily bars found to resolve --asof: {sqlite_path}", file=sys.stderr)
        return 1
    except (SQLiteSchemaError, sqlite3.Error, OSError) as error:
        print(f"error: market snapshot read failed: {error}", file=sys.stderr)
        return 1
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
    parser.add_argument(
        "--current-listing-master",
        type=Path,
        metavar="PATH",
        help="pinned current listings for breadth and sectors; omitted means unconfirmed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return market_snapshot_command(
        asof=args.asof,
        weeks=args.weeks,
        sqlite_path=args.sqlite_path,
        output_format=args.format,
        current_listing_master=args.current_listing_master,
    )
