"""Internal operational entrypoint for production batch jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from importlib import import_module

from baibai_engine.storage.paths import StoreLayoutError, reject_noncanonical_store_paths

Command = Callable[[list[str] | None], int]

# Commands are resolved only after selection. Listing/help never opens unrelated stores or providers.
_COMMANDS = {
    "model-integration": "baibai_jobs.integration.models",
    "external-assessment": "baibai_jobs.acquisition.external_assessments.cli",
    "tradingview": "baibai_jobs.integration.local_tradingview",
    "tradingview-source": "baibai_jobs.acquisition.tradingview.cli",
    "acquire-economic": "baibai_jobs.acquisition.economic_cli",
    "maintain-market": "baibai_jobs.maintenance.market.cli",
    "validate-market-context-stores": "baibai_jobs.observability.source_stores",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs")
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
    command: Command = import_module(_COMMANDS[args.command]).main
    return command(args.arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
