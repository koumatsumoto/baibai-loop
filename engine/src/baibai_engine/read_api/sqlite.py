"""Shared read-only access to the application DB for every read_api query."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from baibai_engine.foundation.sqlite_read import (
    connect_read_only as connect_read_only,
)
from baibai_engine.foundation.sqlite_read import (
    is_missing_table_error as is_missing_table_error,
)
from baibai_engine.foundation.sqlite_read import (
    is_unwritten_store,
    read_rows,
)
from baibai_engine.storage.application.read import (
    connect_read_only as connect_application_read_only,
)

__all__ = ["is_unwritten_store", "read_rows"]


def read_application_rows(
    path: Path, sql: str, parameters: Sequence[object] = ()
) -> list[sqlite3.Row]:
    return read_rows(path, sql, parameters, connector=connect_application_read_only)
