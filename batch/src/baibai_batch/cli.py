"""Internal operational entrypoint for production batch jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from baibai_batch.jobs.daily import main as daily_main
from baibai_batch.jobs.tradingview import main as tradingview_main
from baibai_batch.validation.market_context_stores import (
    main as validate_market_context_stores_main,
)
from baibai_engine.batch_api import (
    StoreLayoutError,
    external_assessment_acquisition_main,
    reject_noncanonical_store_paths,
)

Command = Callable[[list[str] | None], int]

_COMMANDS: dict[str, Command] = {
    "daily": daily_main,
    "external-assessment": external_assessment_acquisition_main,
    "tradingview": tradingview_main,
    "validate-market-context-stores": validate_market_context_stores_main,
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
            reject_noncanonical_store_paths()
        except StoreLayoutError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    return _COMMANDS[args.command](args.arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
