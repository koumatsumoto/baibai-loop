"""Canonical repository-relative paths shared by engine read and write surfaces."""

from pathlib import Path

APPLICATION_DB_PATH = Path("stores/application/baibai.sqlite")
MARKET_DB_PATH = Path("stores/market/market.sqlite")
RUNS_DB_PATH = Path("stores/screening/runs.sqlite")
MACRO_DB_PATH = Path("stores/macro/macro.sqlite")
CALIBRATION_DIR = Path("stores/screening/calibration")
SCREENING_RULES_PATH = Path("method/screening/rules/2026-07-06T000000+0900.yaml")
MACRO_READING_RULES_PATH = Path("method/macro/reading/2026-08-01T100000+0900.yaml")

_LEGACY_STORE_PATHS = (
    Path("data/app/baibai.sqlite"),
    Path("data/screening/market.sqlite"),
    Path("data/indicators/macro.sqlite"),
    Path("data/screening/runs.sqlite"),
    Path("data/screening/calibration"),
)


class LegacyStorePathError(RuntimeError):
    """Runtime state remains under a retired path and could create a second writer."""


def reject_legacy_store_paths(root: Path = Path()) -> None:
    """Fail before a runtime can read or write stores while retired copies exist."""

    present = [path for path in _LEGACY_STORE_PATHS if (root / path).exists()]
    if present:
        rendered = ", ".join(str(path) for path in present)
        raise LegacyStorePathError(
            f"retired store path exists ({rendered}); complete the data -> stores migration "
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
    "LegacyStorePathError",
    "reject_legacy_store_paths",
]
