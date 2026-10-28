"""Market Contextの機械的な指標読取・Readingの入口。"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable
from typing import cast

_COMMANDS = {
    "snapshot": (
        "baibai_engine.cli.market_snapshot",
        "market and sector snapshot from retained prices",
    ),
    "indicators": (
        "baibai_engine.cli.economic_indicators",
        "saved economic indicator observations",
    ),
    "reading": (
        "baibai_engine.cli.market_reading",
        "mechanical reading of saved observations",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine market-context")
    groups = parser.add_subparsers(dest="group", required=True)
    for name, (_, description) in _COMMANDS.items():
        groups.add_parser(name, help=description, add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] not in _COMMANDS:
        build_parser().parse_args(arguments)
        return 0
    module, _ = _COMMANDS[arguments[0]]
    run = cast(Callable[[list[str]], int], importlib.import_module(module).main)
    return run(arguments[1:])
