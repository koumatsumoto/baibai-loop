"""Application database path resolution without importing writer infrastructure."""

from __future__ import annotations

import os
from pathlib import Path

from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH

DEFAULT_DB_PATH = APPLICATION_DB_PATH


def database_path(path: Path | None = None) -> Path:
    """Resolve the canonical application DB path without creating it."""

    if path is not None:
        return path.expanduser()
    configured = os.environ.get("BAIBAI_DB")
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


__all__ = ["DEFAULT_DB_PATH", "database_path"]
