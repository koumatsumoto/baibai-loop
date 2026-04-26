from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .sync import sync_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync", help="sync research decisions into JSONL ledgers")
    sync_parser.add_argument("--root", type=Path, default=Path.cwd())
    sync_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync":
        result = sync_ledger(args.root, dry_run=args.dry_run)
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.dry_run:
            for line in result.diff_lines:
                print(line)
        print(
            f"paper={result.paper_count} skipped={result.skipped_count}"
            + (" dry_run=true" if args.dry_run else "")
        )
        return 0
    raise AssertionError(f"unreachable command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
