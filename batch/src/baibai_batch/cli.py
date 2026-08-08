"""Internal operational entrypoint for production batch jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from baibai_batch.jobs.daily import main as daily_main
from baibai_batch.jobs.history_backfill import main as history_backfill_main
from baibai_batch.jobs.watchdog import main as watchdog_main
from baibai_batch.validation.macro_stores import main as validate_macro_stores_main
from baibai_engine.batch_api import LegacyStorePathError, reject_legacy_store_paths

Command = Callable[[list[str] | None], int]

_COMMANDS: dict[str, Command] = {
    "daily": daily_main,
    "history-backfill": history_backfill_main,
    "watchdog": watchdog_main,
    "validate-macro-stores": validate_macro_stores_main,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-batch")
    parser.add_argument("command", choices=tuple(_COMMANDS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not {"-h", "--help"}.intersection(args.arguments):
        try:
            reject_legacy_store_paths()
        except LegacyStorePathError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    return _COMMANDS[args.command](args.arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
