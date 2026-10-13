"""Pure Research calculation: one typed input file, JSON on stdout, no writes."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import DecimalException
from pathlib import Path

import yaml

from baibai_engine.foundation.yaml_io import safe_load

from .calculation import CalculationInput, calculate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine research calculate")
    parser.add_argument(
        "inputs",
        type=Path,
        help="Snapshot, ratio definitions without value, and optional Valuation (YAML/JSON)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raw = safe_load(args.inputs.read_text(encoding="utf-8"))
        result = calculate(CalculationInput.model_validate(raw))
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    except (OSError, yaml.YAMLError, ValueError, DecimalException, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(encoded)
    return 0
