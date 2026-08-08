"""Canonical repository-relative paths shared by engine read and write surfaces."""

import os
from collections.abc import Iterable
from pathlib import Path

APPLICATION_DB_PATH = Path("stores/application/baibai.sqlite")
MARKET_DB_PATH = Path("stores/market/market.sqlite")
RUNS_DB_PATH = Path("stores/screening/runs.sqlite")
MACRO_DB_PATH = Path("stores/macro/macro.sqlite")
CALIBRATION_DIR = Path("stores/screening/calibration")
SCREENING_RULES_PATH = Path("method/screening/rules/2026-07-06T000000+0900.yaml")
MACRO_READING_RULES_PATH = Path("method/macro/reading/2026-08-01T100000+0900.yaml")

STORE_LAYOUT_MAPPINGS = (
    (Path("data/app/baibai.sqlite"), APPLICATION_DB_PATH),
    (Path("data/screening/market.sqlite"), MARKET_DB_PATH),
    (Path("data/indicators/macro.sqlite"), MACRO_DB_PATH),
    (Path("data/screening/runs.sqlite"), RUNS_DB_PATH),
    (Path("data/screening/calibration"), CALIBRATION_DIR),
)


class LegacyStorePathError(RuntimeError):
    """Runtime state remains under a retired path and could create a second writer."""


def reject_legacy_store_paths(root: Path = Path(), *, raw_arguments: Iterable[str] = ()) -> None:
    """Fail before a runtime can read or write a store at a retired path.

    Existing files detect an incomplete migration. Environment overrides and raw
    CLI arguments are checked as prospective paths as well, so stale configuration
    cannot recreate a retired writer after the old file has been moved away.
    """

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

    resolved_root = root.resolve()
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
        raise LegacyStorePathError(
            f"retired store path exists or is configured ({rendered}); "
            "complete the data -> stores migration "
            "before running Baibai Loop"
        )


__all__ = [
    "APPLICATION_DB_PATH",
    "CALIBRATION_DIR",
    "MACRO_DB_PATH",
    "MACRO_READING_RULES_PATH",
    "MARKET_DB_PATH",
    "RUNS_DB_PATH",
    "SCREENING_RULES_PATH",
    "STORE_LAYOUT_MAPPINGS",
    "LegacyStorePathError",
    "reject_legacy_store_paths",
]
