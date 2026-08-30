"""Canonical repository-relative paths shared by engine read and write surfaces."""

from pathlib import Path

APPLICATION_DB_PATH = Path("stores/application/baibai.sqlite")
MARKET_DB_PATH = Path("stores/market/market.sqlite")
RUNS_DB_PATH = Path("stores/screening/runs.sqlite")
MACRO_DB_PATH = Path("stores/macro/macro.sqlite")
CALIBRATION_DIR = Path("stores/screening/calibration")
ER_LEVEL_CALIBRATION_CONTEXT_PATH = Path("reports/published/er-level-calibration-latest.yaml")
SCREENING_RULES_PATH = Path("method/screening/rules/2026-08-30T215359+0900.yaml")
MACRO_READING_RULES_PATH = Path("method/macro/reading/2026-08-01T100000+0900.yaml")


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


def reject_noncanonical_store_paths(root: Path = Path()) -> None:
    """Fail when a relative store path is resolved outside the repository root."""
    resolved_root = root.resolve()
    root_error = repository_root_error(resolved_root, label="the store root")
    if root_error is not None:
        raise StoreLayoutError(
            f"{root_error}; run Baibai Loop from the repository root so the relative "
            "store paths resolve to the canonical stores instead of creating new ones"
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
    "StoreLayoutError",
    "reject_noncanonical_store_paths",
    "repository_root_error",
]
