"""Legacy candidates loader retained for the final migration runner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_engine.foundation.yaml_io import safe_load

from .store import ScreeningRunStore


def import_screening_runs(
    source_root: Path,
    *,
    db_path: Path | None = None,
) -> tuple[int, int]:
    paths = sorted(source_root.glob("*/*/*.yaml"))
    payloads: list[Mapping[str, object]] = []
    for path in paths:
        raw = safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(f"screening run must be a mapping: {path}")
        payloads.append(raw)
    return ScreeningRunStore(db_path).import_runs(payloads)


__all__ = ["import_screening_runs"]
