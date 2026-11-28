"""Read-only workflow counts and opt-in shape diagnosis of fixed TradingView receipts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_engine.storage.sqlite.source_cache.schema import (
    validate_current_schema as validate_market_schema,
)

MAX_DIAGNOSTIC_RECEIPTS = 100


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


def diagnose_receipts(
    sqlite_path: Path, snapshot_date: date, receipt_ids: Sequence[str]
) -> dict[str, object]:
    """Count native field shapes in a sealed SQLite copy, not quality/coverage."""
    if not 1 <= len(receipt_ids) <= MAX_DIAGNOSTIC_RECEIPTS or len(set(receipt_ids)) != len(
        receipt_ids
    ):
        raise ValueError("select one attempt with distinct receipt IDs within the limit")

    # The workflow's normal two-count command does not need the MCP decoder.
    from baibai_engine.data_collection.providers.tradingview.accounting import (
        BatchAccountingError,
        account_batch,
    )
    from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL_IDENTIFIERS
    from baibai_engine.data_collection.providers.tradingview.receipts import read_tool_results
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
        contract,
        decode,
        observations,
    )

    fields = {
        name: {"absent": 0, "null": 0, "non_null": 0, "receipts_with_absence": 0}
        for name in contract.FIELDS
    }
    outcomes: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    unique_symbols: set[str] = set()
    appearances = rows_with_extra_keys = 0
    master_asof = None
    # The packet has no outstanding WAL, so immutable mode also prevents sidecar writes.
    fixed = sqlite_path.resolve()
    if any(fixed.with_name(fixed.name + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("SQLite copy still has a journal")
    uri = fixed.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        # A sealed packet needs only the exact receipt table, not unrelated Market tables.
        for receipt_id in receipt_ids:
            (receipt,) = read_tool_results(conn, (receipt_id,))
            symbols = receipt.requested_symbols
            if (
                receipt.snapshot_date != snapshot_date
                or (master_asof is not None and receipt.master_asof != master_asof)
                or receipt.tool_name not in BATCH_TOOL_IDENTIFIERS
                or receipt.requested_columns != tuple(contract.FIELDS.values())
                or not 1 <= len(symbols) <= 50
                or len(set(symbols)) != len(symbols)
            ):
                raise ValueError("selection differs from the fixed forecast request")
            master_asof = receipt.master_asof
            appearances += len(symbols)
            unique_symbols.update(symbols)
            try:
                data, missing = account_batch(
                    decode.decode_tool_result(receipt.result_json), symbols
                )
            except (BatchAccountingError, observations.ProviderPayloadError) as exc:
                outcomes[exc.reason] += 1
                continue
            except observations.ProviderHTTPError as exc:
                code = exc.status_code
                outcomes[
                    f"provider_http_{code}" if code in {401, 403, 429} else "provider_http_other"
                ] += 1
                continue
            except observations.FetchError:
                outcomes["tool_error"] += 1
                continue

            shapes["explicit_missing"] += len(missing)
            absent: set[str] = set()
            non_object = False
            for row in data.values():
                if not isinstance(row, dict):
                    shapes["non_object"] += 1
                    non_object = True
                    continue
                present = 0
                for name, key in contract.FIELDS.items():
                    if key not in row:
                        fields[name]["absent"] += 1
                        absent.add(name)
                    else:
                        present += 1
                        fields[name]["null" if row[key] is None else "non_null"] += 1
                rows_with_extra_keys += len(row) > present
                shape = (
                    "all_fields"
                    if present == len(contract.FIELDS)
                    else "some_fields"
                    if present
                    else "no_fields"
                )
                shapes[shape] += 1
            for name in absent:
                fields[name]["receipts_with_absence"] += 1
            outcome = (
                "all_explicit_missing"
                if not data
                else "incomplete_shape"
                if absent or non_object
                else "complete_shape"
            )
            outcomes[outcome] += 1
    return {
        "selected_receipts": len(receipt_ids),
        "requested_symbol_appearances": appearances,
        "distinct_requested_symbols": len(unique_symbols),
        "receipt_outcomes": dict(outcomes),
        "row_shapes": dict(shapes),
        "object_rows_with_extra_keys": rows_with_extra_keys,
        "fields": fields,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument(
        "--diagnose-receipt",
        action="append",
        metavar="ID",
        help=f"inspect one sealed local attempt (repeat up to {MAX_DIAGNOSTIC_RECEIPTS} times)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.diagnose_receipt:
        if args.sqlite is None:
            parser.error("receipt diagnosis requires an explicit --sqlite copy")
        try:
            report = diagnose_receipts(args.sqlite, args.asof, args.diagnose_receipt)
        except (TypeError, ValueError, sqlite3.Error, SQLiteSchemaError):
            # Even a missing-receipt exception can include the supplied private ID.
            parser.exit(2, "TradingView receipt diagnosis: invalid selection or local input\n")
        print(json.dumps(report, sort_keys=True))
    else:
        print(*count_layer_rows(args.sqlite or MARKET_DB_PATH, args.asof))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
