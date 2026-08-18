"""Fail before store-transfer scripts can operate on a split repository layout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from baibai_engine.batch_api import StoreLayoutError, reject_noncanonical_store_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        reject_noncanonical_store_paths(args.root.resolve())
    except StoreLayoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
