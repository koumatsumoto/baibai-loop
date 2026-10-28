"""保存済み評価の価格watchを表示する入口。注文は行わない。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml

from baibai_engine.investment_judgment.contracts import AssessmentError
from baibai_engine.investment_judgment.stock_assessment.store import ResearchValidationError
from baibai_engine.investment_judgment.trade_proposal.price_watch import (
    ResearchPriceWatchError,
    build_watch,
)
from baibai_engine.portfolio.ledger.events import PortfolioLedgerError
from baibai_engine.portfolio.ledger.read import LedgerConflictError, LedgerSchemaError
from baibai_engine.storage.paths import (
    APPLICATION_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baibai_engine.cli.price_watch",
        description=(
            "Emit a read-only YAML watch for the latest published Stock Assessment of every "
            "ticker, with supported, uncertain, or unsupported stance. "
            "This command never "
            "proposes an order or updates canonical records."
        ),
    )
    parser.add_argument("--db", type=Path, default=APPLICATION_DB_PATH)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--asof", type=_date_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reject_noncanonical_store_paths()
    except StoreLayoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        payload = build_watch(
            app_db_path=args.db,
            sqlite_path=args.sqlite_path,
            asof=args.asof,
        )
    except (
        PortfolioLedgerError,
        LedgerConflictError,
        LedgerSchemaError,
        ResearchPriceWatchError,
        SQLiteSchemaError,
        # A published assessment without a recorded identity refuses through this one.
        AssessmentError,
        ResearchValidationError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
