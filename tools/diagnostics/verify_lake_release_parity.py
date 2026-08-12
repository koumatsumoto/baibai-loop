"""Compare a screening pass over the legacy store with one over a lake release.

This is a migration diagnostic for issue #917. It runs screening twice for one
as-of — once over the current ``market.sqlite`` and once over a store whose
lake-published tables come from a fixed release projection — and reports every
difference in candidates, metrics, and selection. It exits non-zero when the two
disagree, and it changes no runtime store: both sides run against throwaway copies
in a workspace directory.

It lives here rather than under ``baibai-engine screening`` because it exists only
while the two paths coexist. When the release becomes the authority there is no
legacy side left to compare, and this file is deleted rather than deprecated.

The comparison holds every input constant except the source of the migrated
datasets, so the report always lists which tables came from the release and which
still come from the legacy store. The second list is what a cutover still has to
move.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.market.lake.projection import ProjectionError
from baibai_engine.screening.config import (
    DEFAULT_SQLITE_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from baibai_engine.screening.lake_shadow import LakeShadowError, run_lake_shadow_parity
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

JST = ZoneInfo("Asia/Tokyo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify_lake_release_parity", description=__doc__)
    parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    parser.add_argument(
        "--projection", required=True, help="local SQLite projection of one fixed L1 release"
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"legacy SQLite store (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    parser.add_argument(
        "--workspace",
        help="directory for the two throwaway stores (default: a temporary directory)",
    )
    parser.add_argument("--app-db", help="application DB both sides read for select")
    parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    parser.add_argument("--top", type=int, default=10, help="select --top (default: 10)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ScreeningConfig.from_env()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    from baibai_engine.appdb import database_path

    with contextlib.ExitStack() as stack:
        workspace = (
            Path(args.workspace)
            if args.workspace
            else Path(stack.enter_context(tempfile.TemporaryDirectory()))
        )
        try:
            report = run_lake_shadow_parity(
                asof=date.fromisoformat(args.asof),
                legacy_sqlite=Path(args.sqlite_path),
                projection=Path(args.projection),
                workspace=workspace,
                config=config,
                rules=load_screening_rules(Path(args.rules_path)),
                now=datetime.now(JST),
                app_db_path=Path(args.app_db) if args.app_db else database_path(),
                select_top=args.top,
                stdout=sys.stdout,
            )
        except (LakeShadowError, ProjectionError, OSError, ValueError, sqlite3.Error) as error:
            print(f"lake parity failed: {error}", file=sys.stderr)
            return 1
    yaml.safe_dump(report.as_dict(), sys.stdout, sort_keys=False, allow_unicode=True)
    return 0 if report.matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
