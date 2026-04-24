from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import ConfigError, ScreeningConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m baibai_loop.screening.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run weekly screening")
    run_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap raw caches before provider logic is fully wired",
    )
    bootstrap_parser.add_argument("--start", required=True, help="start date (YYYY-MM-DD)")
    bootstrap_parser.add_argument("--end", required=True, help="end date (YYYY-MM-DD)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "run":
        _parse_iso_date(args.asof)
        print("run command scaffolded; provider implementation is not wired yet", file=sys.stderr)
        return 1

    if args.command == "bootstrap-cache":
        start = _parse_iso_date(args.start)
        end = _parse_iso_date(args.end)
        if start > end:
            print("--start must be on or before --end", file=sys.stderr)
            return 1
        print(
            "bootstrap-cache command scaffolded; provider implementation is not wired yet",
            file=sys.stderr,
        )
        return 1

    parser.error("unknown command")
    return 2


def _parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO date: {raw}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
