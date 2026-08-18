"""Canonical repository-relative paths shared by engine read and write surfaces."""

import os
from collections.abc import Iterable
from pathlib import Path

APPLICATION_DB_PATH = Path("stores/application/baibai.sqlite")
MARKET_DB_PATH = Path("stores/market/market.sqlite")
RUNS_DB_PATH = Path("stores/screening/runs.sqlite")
MACRO_DB_PATH = Path("stores/macro/macro.sqlite")
CALIBRATION_DIR = Path("stores/screening/calibration")
ER_LEVEL_CALIBRATION_CONTEXT_PATH = Path("reports/published/er-level-calibration-latest.yaml")
SCREENING_RULES_PATH = Path("method/screening/rules/2026-07-06T000000+0900.yaml")
MACRO_READING_RULES_PATH = Path("method/macro/reading/2026-08-01T100000+0900.yaml")

STORE_LAYOUT_MAPPINGS = (
    (Path("data/app/baibai.sqlite"), APPLICATION_DB_PATH),
    (Path("data/screening/market.sqlite"), MARKET_DB_PATH),
    (Path("data/indicators/macro.sqlite"), MACRO_DB_PATH),
    (Path("data/screening/runs.sqlite"), RUNS_DB_PATH),
    (Path("data/screening/calibration"), CALIBRATION_DIR),
)


class StoreLayoutError(RuntimeError):
    """Runtime state would live outside the canonical store layout."""


def repository_root_error(root: Path, *, label: str) -> str | None:
    """Describe why ``root`` is not the repository root, or ``None`` when it is.

    ``label`` names what the caller resolved the root from, so a command that takes
    it as an argument reports the argument the operator has to fix.
    """

    for marker, present in (
        ("pyproject.toml", (root / "pyproject.toml").is_file()),
        ("method/", (root / "method").is_dir()),
    ):
        if not present:
            return f"{label} does not contain {marker}: {root}"
    return None


def reject_noncanonical_store_paths(
    root: Path = Path(), *, raw_arguments: Iterable[str] = ()
) -> None:
    """Fail before a runtime can read or write a store outside the canonical layout.

    The root is checked first because every path in this module is relative to it.
    Scanning the wrong tree reports "no retired path" whatever the repository holds,
    and sqlite creates a missing store rather than refusing, so a working directory
    one level off forks the state silently instead of failing.

    Retired paths are then rejected both where they exist and where configuration
    only names them, so stale settings cannot recreate a retired writer after the
    old file has been moved away.
    """

    # The default root is the working directory, which renders as ".": report where
    # that actually is, because the whole point is that the operator is elsewhere.
    resolved_root = root.resolve()
    root_error = repository_root_error(resolved_root, label="the store root")
    if root_error is not None:
        raise StoreLayoutError(
            f"{root_error}; run Baibai Loop from the repository root so the relative "
            "store paths resolve to the canonical stores instead of creating new ones"
        )

    legacy_paths = tuple(legacy for legacy, _current in STORE_LAYOUT_MAPPINGS)
    present = [path for path in legacy_paths if os.path.lexists(root / path)]
    configured = [
        Path(value).expanduser()
        for name in ("BAIBAI_DB", "BAIBAI_RUNS_DB")
        if (value := os.environ.get(name))
    ]
    for argument in raw_arguments:
        candidate = argument.split("=", maxsplit=1)[-1]
        configured.append(Path(candidate).expanduser())

    retired_targets = {(resolved_root / path).resolve(): path for path in legacy_paths}
    selected = [
        retired_targets[resolved]
        for path in configured
        if (resolved := (path if path.is_absolute() else resolved_root / path).resolve())
        in retired_targets
    ]
    rejected = tuple(dict.fromkeys([*present, *selected]))
    if rejected:
        rendered = ", ".join(str(path) for path in rejected)
        raise StoreLayoutError(
            f"retired store path exists or is configured ({rendered}); "
            "complete the data -> stores migration "
            "before running Baibai Loop"
        )


__all__ = [
    "APPLICATION_DB_PATH",
    "CALIBRATION_DIR",
    "ER_LEVEL_CALIBRATION_CONTEXT_PATH",
    "MACRO_DB_PATH",
    "MACRO_READING_RULES_PATH",
    "MARKET_DB_PATH",
    "RUNS_DB_PATH",
    "SCREENING_RULES_PATH",
    "STORE_LAYOUT_MAPPINGS",
    "StoreLayoutError",
    "reject_noncanonical_store_paths",
    "repository_root_error",
]
