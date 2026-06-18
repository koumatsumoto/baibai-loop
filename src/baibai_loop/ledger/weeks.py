"""Weekly candidates discovery and loading shared by the ledger telemetry tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_loop.yaml_io import safe_load


@dataclass(frozen=True, slots=True)
class WeekSpec:
    asof: date
    candidates_path: Path
    is_holdout: bool = False


def discover_week_specs(candidates_root: Path, holdout_weeks: int = 0) -> list[WeekSpec]:
    """Discover weekly candidate files under ``candidates_root`` sorted by asof.

    Expects the canonical ``<root>/<YYYY>/<MM>/<YYYY-MM-DD>.yaml`` layout. The
    last ``holdout_weeks`` weeks are flagged as hold-out so the artifact can
    separate the tuning window from the evaluation window.
    """
    specs: list[WeekSpec] = []
    for path in sorted(candidates_root.rglob("*.yaml")):
        try:
            asof = date.fromisoformat(path.stem)
        except ValueError:
            continue
        specs.append(WeekSpec(asof=asof, candidates_path=path))
    specs.sort(key=lambda spec: spec.asof)
    if holdout_weeks > 0:
        cutoff = len(specs) - holdout_weeks
        specs = [
            WeekSpec(spec.asof, spec.candidates_path, is_holdout=index >= cutoff)
            for index, spec in enumerate(specs)
        ]
    return specs


def load_week_candidates(
    candidates_path: Path,
    *,
    payload_cache: dict[Path, Mapping[str, object]] | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Load a weekly candidates YAML and return its candidate mappings.

    ``payload_cache`` lets a caller share parsed payloads when the same YAML is
    read more than once (``run_replay`` reads week N as the current sweep and
    again as the previous-week reference for week N+1). The cache key is the
    resolved path so callers that mix relative/absolute paths still hit.
    """
    payload = _load_payload(candidates_path, payload_cache)
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid candidates YAML: {candidates_path}")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, str | bytes):
        raise ValueError(f"candidates list missing: {candidates_path}")
    return tuple(item for item in raw_candidates if isinstance(item, Mapping))


def _load_payload(
    path: Path,
    payload_cache: dict[Path, Mapping[str, object]] | None,
) -> Mapping[str, object] | None:
    if payload_cache is None:
        loaded = safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, Mapping) else None
    key = path.resolve()
    cached = payload_cache.get(key)
    if cached is not None:
        return cached
    loaded = safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, Mapping):
        payload_cache[key] = loaded
        return loaded
    return None
