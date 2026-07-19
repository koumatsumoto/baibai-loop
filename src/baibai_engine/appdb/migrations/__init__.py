"""Numbered, forward-only application database migrations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT
            """,
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version

__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration"]
