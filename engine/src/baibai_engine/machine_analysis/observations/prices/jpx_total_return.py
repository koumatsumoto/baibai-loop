"""保存済み公式JPX total-return観測ファイルの読取adapter。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

import baibai_engine.machine_analysis.observations.prices.benchmark_observation as benchmark
from baibai_engine.primitives.yaml import safe_load


class BenchmarkObservationError(ValueError):
    """Raised when a JPX benchmark observation is incomplete or malformed."""


def load_benchmark_observation(path: Path) -> benchmark.BenchmarkObservation:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise BenchmarkObservationError(f"failed to read benchmark observation: {error}") from error
    if not isinstance(raw, dict):
        raise BenchmarkObservationError("benchmark observation root must be a mapping")
    try:
        return benchmark.BenchmarkObservation.model_validate(raw)
    except ValidationError as error:
        raise BenchmarkObservationError(str(error)) from error
