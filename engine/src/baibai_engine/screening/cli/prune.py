"""Manual retention command for the rebuildable screening run cache."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.screening.run_store import ScreeningRunStore


def prune_command(
    *,
    keep: int,
    runs_db_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        result = ScreeningRunStore(runs_db_path).prune(keep=keep)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"screening prune: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(asdict(result), out, sort_keys=False)
    return 0


__all__ = ["prune_command"]
