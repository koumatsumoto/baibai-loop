"""Explicit, local-only DuckDB sessions for verified lake files."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class LakeSession:
    """One local query connection, owned and closed by its context."""

    connection: duckdb.DuckDBPyConnection


@contextmanager
def lake_session() -> Iterator[LakeSession]:
    """Read local files without loading or downloading remote extensions."""

    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET autoinstall_known_extensions = false")
        connection.execute("SET autoload_known_extensions = false")
        yield LakeSession(connection=connection)
    finally:
        connection.close()
