"""Loaders, vocabularies, and small helpers shared across the research validators."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

from baibai_loop.coerce import optional_float
from baibai_loop.validate.domain import (
    repo_root_for,
    repository_ref_error,
    resolve_repository_ref,
)

_KNOWN_MACRO_CONTEXT_FRESHNESS = {"current", "stale", "future"}

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(path: Path) -> object:
    stat = path.stat()
    return _load_yaml_cached(path.resolve().as_posix(), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _load_yaml_cached(path: str, mtime_ns: int, size: int) -> object:
    del mtime_ns, size
    # _YAML_LOADER is CSafeLoader or SafeLoader; keep yaml.load for the C loader path.
    return yaml.load(  # nosec B506
        Path(path).read_text(encoding="utf-8"), Loader=_YAML_LOADER
    )


def _research_record_date(
    front_matter: Mapping[str, object], *, path: Path | None = None
) -> date | None:
    record_dt = _parse_datetime(front_matter.get("published_at")) or _parse_datetime(
        front_matter.get("recorded_at")
    )
    if record_dt is not None:
        return record_dt.date()
    decision = front_matter.get("research_decision")
    if isinstance(decision, Mapping):
        decision_dt = _parse_datetime(decision.get("decided_at"))
        if decision_dt is not None:
            return decision_dt.date()
    if path is not None:
        try:
            return date.fromisoformat(path.name[:10])
        except ValueError:
            return None
    return None


def _gate_boundary_date(front_matter: Mapping[str, object], *, path: Path | None) -> date | None:
    """Return the latest of published_at / recorded_at / decided_at / filename.

    Standard `_research_record_date` uses the first available source, which lets
    a backdated `published_at` slip past an effective-date gate. For
    gate-boundary comparisons specifically, take the latest signal so a wider
    surface area (including the tamper-resistant filename) governs whether the
    gate applies. The filename is git-reviewable and the naming convention is
    enforced elsewhere, so it is the most tamper-resistant source.
    """
    candidates: list[date] = []
    record_dt = _parse_datetime(front_matter.get("published_at"))
    if record_dt is not None:
        candidates.append(record_dt.date())
    recorded_dt = _parse_datetime(front_matter.get("recorded_at"))
    if recorded_dt is not None:
        candidates.append(recorded_dt.date())
    decision = front_matter.get("research_decision")
    if isinstance(decision, Mapping):
        decided_dt = _parse_datetime(decision.get("decided_at"))
        if decided_dt is not None:
            candidates.append(decided_dt.date())
    if path is not None:
        with contextlib.suppress(ValueError):
            candidates.append(date.fromisoformat(path.name[:10]))
    return max(candidates) if candidates else None


def _parse_date_value(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed


def _close(value: object, expected: float, *, tolerance: float) -> bool:
    number = optional_float(value)
    return number is not None and abs(number - expected) <= tolerance


def _format_path(parts: Iterable[object]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)


def _is_repository_research_record(path: Path) -> bool:
    root = repo_root_for(path)
    try:
        path.resolve().relative_to((root / "records/05-research").resolve())
    except ValueError:
        return False
    return True


def _resolve_record_ref(
    path: Path,
    ref: str,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> Path | None:
    root = repo_root_for(path)
    if repository_ref_error(ref, root=root) is not None:
        return None
    candidate = resolve_repository_ref(root, ref)
    if not ref.startswith(prefixes) or candidate.suffix not in suffixes:
        return None
    return candidate if candidate.is_file() else None
