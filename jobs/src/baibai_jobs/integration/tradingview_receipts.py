"""Read-only receipt delta and the narrow failure-publication policy."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.schema import (
    validate_current_schema as validate_market_schema,
)

PUBLISHABLE_FAILURES = frozenset(
    {
        "provider_rate_limit",
        "provider_response",
        "provider_transport",
        "auth",
        "secret_persistence",
        "time_guard",
        "timeout",
    }
)
KNOWN_FAILURES = PUBLISHABLE_FAILURES | {"storage", "internal"}


def count_layer_rows(sqlite_path: Path, snapshot_date: date) -> tuple[int, int]:
    """Read both layer counts from one validated, read-only market snapshot."""
    uri = sqlite_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            validate_market_schema(conn)
            receipt_row = conn.execute(
                "SELECT COUNT(*) FROM tradingview_source_receipts WHERE snapshot_date=?",
                (snapshot_date.isoformat(),),
            ).fetchone()
            snapshot_row = conn.execute(
                "SELECT COUNT(*) FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
                (snapshot_date.isoformat(),),
            ).fetchone()
            assert receipt_row is not None
            assert snapshot_row is not None
            return int(receipt_row[0]), int(snapshot_row[0])
        finally:
            conn.rollback()


def can_publish_failure_receipts(
    category: str | None, before: int, after: int, rows_before: int, rows_after: int
) -> bool:
    return (
        category in PUBLISHABLE_FAILURES
        and before >= 0
        and after > before
        and rows_before >= 0
        and rows_after == rows_before
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--sqlite", type=Path, default=MARKET_DB_PATH)
    args = parser.parse_args(argv)
    print(*count_layer_rows(args.sqlite, args.asof))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
