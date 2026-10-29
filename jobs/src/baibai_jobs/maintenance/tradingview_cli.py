"""Explicit offline reconstruction of retained TradingView receipts; no live acquisition."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from baibai_jobs.maintenance.tradingview_rebuild import RebuildError, rebuild_from_receipts
from baibai_jobs.observability.tradingview import failure_line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs tradingview-source")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild = sub.add_parser("rebuild", help="rebuild one date from explicit saved MCP receipts")
    rebuild.add_argument("--sqlite", type=Path, required=True)
    rebuild.add_argument("--asof", type=date.fromisoformat, required=True)
    rebuild.add_argument("--receipt-id", action="append", required=True)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = rebuild_from_receipts(args.sqlite, args.asof, args.receipt_id)
    except RebuildError as exc:
        print(json.dumps({"status": "failed", "rejected_receipts": exc.rejected}), file=sys.stderr)
        return 1
    except Exception as exc:
        print(failure_line(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
