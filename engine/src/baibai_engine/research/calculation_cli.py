"""Pure Research calculation: one typed input file, JSON on stdout, no writes."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import DecimalException
from pathlib import Path

import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.valuation.cash_share_cb import CashShareCBInput, calculate_cash_share_cb

from .calculation import CalculationInput, calculate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine research calculate")
    parser.add_argument(
        "inputs",
        nargs="?",
        type=Path,
        help="Snapshot, ratio definitions without value, and optional Valuation (YAML/JSON)",
    )
    parser.add_argument(
        "--cash-share-cb",
        type=Path,
        help="Read-only face-cash plus excess-share CB scenario (YAML/JSON)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.inputs is None) == (args.cash_share_cb is None):
        print("error: supply exactly one of inputs or --cash-share-cb", file=sys.stderr)
        return 2
    try:
        path = args.inputs if args.inputs is not None else args.cash_share_cb
        raw = safe_load(path.read_text(encoding="utf-8"))
        result = (
            calculate(CalculationInput.model_validate(raw))
            if args.inputs is not None
            else calculate_cash_share_cb(CashShareCBInput.model_validate(raw))
        )
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    except (OSError, yaml.YAMLError, ValueError, DecimalException, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(encoded)
    return 0
