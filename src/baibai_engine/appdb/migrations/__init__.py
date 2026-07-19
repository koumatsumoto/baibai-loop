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
    Migration(
        version=2,
        statements=(
            """
            CREATE TABLE task (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('open', 'done', 'dropped')),
                kind TEXT NOT NULL CHECK (
                    kind IN ('earnings-review', 'ops', 'follow-up', 'other')
                ),
                ticker TEXT,
                due_date TEXT NOT NULL,
                event_date TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            "CREATE INDEX task_status_due_idx ON task(status, due_date, task_id)",
            "CREATE INDEX task_ticker_idx ON task(ticker) WHERE ticker IS NOT NULL",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version

__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration"]
