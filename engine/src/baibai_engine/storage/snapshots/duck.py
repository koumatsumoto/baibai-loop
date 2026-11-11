"""Explicit, local-only DuckDB sessions for verified lake files."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class LakeSession:
    """One local query connection, owned and closed by its context."""

    connection: duckdb.DuckDBPyConnection


@contextmanager
def lake_session() -> Iterator[LakeSession]:
    """Read local files without loading or downloading remote extensions."""

    profile = os.environ.get("BAIBAI_LAKE_QUERY_PROFILE", "")
    if profile not in {"", "small-runner"}:
        raise ValueError("unsupported lake query profile")
    directory = (
        tempfile.TemporaryDirectory(prefix="baibai-lake-query-") if profile else nullcontext(None)
    )
    with directory as temporary:
        connection = (
            duckdb.connect(
                ":memory:",
                config={
                    "memory_limit": "1GB",
                    "threads": 2,
                    "max_temp_directory_size": "4GB",
                    "temp_directory": temporary,
                },
            )
            if temporary is not None
            else duckdb.connect(":memory:")
        )
        try:
            connection.execute("SET autoinstall_known_extensions = false")
            connection.execute("SET autoload_known_extensions = false")
            yield LakeSession(connection=connection)
        finally:
            connection.close()
