"""Public source-command composition; pure transports remain in data_collection."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine data-collection")
    groups = parser.add_subparsers(dest="group", required=True)
    groups.add_parser("economic", help="list and acquire external economic sources", add_help=False)
    groups.add_parser(
        "market", help="acquire market sources and project retained originals", add_help=False
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "economic":
        from baibai_engine.data_collection.economic.cli import main as economic_main

        return economic_main(arguments[1:])
    if arguments and arguments[0] == "market":
        from baibai_engine.source_maintenance.cli import main as market_main

        return market_main(arguments[1:])
    build_parser().parse_args(arguments)
    return 0
