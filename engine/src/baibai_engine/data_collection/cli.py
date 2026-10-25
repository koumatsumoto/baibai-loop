"""External source collection commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .economic import cli as economic_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine data-collection")
    groups = parser.add_subparsers(dest="group", required=True)
    groups.add_parser("economic", help="list and acquire external economic sources", add_help=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "economic":
        build_parser().parse_args(arguments)
        return 0
    return economic_cli.main(arguments[1:])
