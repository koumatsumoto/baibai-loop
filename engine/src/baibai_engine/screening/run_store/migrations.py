"""Forward-only schema lifecycle for the rebuildable screening run store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from baibai_engine.appdb.json import canonical_json


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]
    transform: Callable[[sqlite3.Connection], None] | None = None


def _strip_embedded_candidates(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT run_revision_id, payload FROM screening_run ORDER BY run_revision_id"
    ).fetchall()
    for run_revision_id, raw_payload in rows:
        payload = json.loads(str(raw_payload))
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            raise sqlite3.IntegrityError(
                f"run payload candidates are unavailable: {run_revision_id}"
            )
        stored_candidates = [
            json.loads(str(row[0]))
            for row in connection.execute(
                """
                SELECT payload FROM screening_candidate
                WHERE run_revision_id = ? ORDER BY ordinal
                """,
                (run_revision_id,),
            ).fetchall()
        ]
        if canonical_json(payload["candidates"]) != canonical_json(stored_candidates):
            raise sqlite3.IntegrityError(
                f"run payload candidates differ from candidate rows: {run_revision_id}"
            )
        del payload["candidates"]
        connection.execute(
            "UPDATE screening_run SET payload = ? WHERE run_revision_id = ?",
            (canonical_json(payload), run_revision_id),
        )


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE screening_run (
                run_revision_id TEXT PRIMARY KEY,
                public_run_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                run_at TEXT NOT NULL,
                universe_size INTEGER NOT NULL CHECK (universe_size >= 0),
                rules_ref TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (public_run_id, run_at)
            ) STRICT
            """,
            """
            CREATE INDEX screening_run_asof_idx
            ON screening_run (asof_date, run_at, run_revision_id)
            """,
            """
            CREATE TABLE screening_candidate (
                run_revision_id TEXT NOT NULL
                    REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                ticker TEXT NOT NULL,
                sector_33 TEXT,
                per_forward REAL,
                per_trailing REAL,
                pbr REAL,
                dividend_yield REAL,
                er_annual REAL,
                payload TEXT NOT NULL,
                PRIMARY KEY (run_revision_id, ticker),
                UNIQUE (run_revision_id, ordinal)
            ) STRICT, WITHOUT ROWID
            """,
            """
            CREATE INDEX screening_candidate_ticker_idx
            ON screening_candidate (ticker, run_revision_id)
            """,
            """
            CREATE TABLE screening_selection (
                selection_id TEXT PRIMARY KEY,
                run_revision_id TEXT NOT NULL
                    REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
                publication_kind TEXT NOT NULL,
                profile TEXT NOT NULL,
                macro_context_id TEXT,
                created_at TEXT NOT NULL,
                source_selection_id TEXT
                    REFERENCES screening_selection(selection_id) ON DELETE RESTRICT,
                payload TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE INDEX screening_selection_lookup_idx
            ON screening_selection (run_revision_id, profile, created_at, selection_id)
            """,
            """
            CREATE TABLE selection_entry (
                selection_id TEXT NOT NULL
                    REFERENCES screening_selection(selection_id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                ticker TEXT NOT NULL,
                selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
                payload TEXT NOT NULL,
                PRIMARY KEY (selection_id, ordinal),
                UNIQUE (selection_id, ticker)
            ) STRICT, WITHOUT ROWID
            """,
        ),
    ),
    Migration(
        version=2,
        statements=("ALTER TABLE screening_run ADD COLUMN application_git_commit TEXT",),
        transform=_strip_embedded_candidates,
    ),
)

RUN_STORE_SCHEMA_VERSION = MIGRATIONS[-1].version


__all__ = ["MIGRATIONS", "RUN_STORE_SCHEMA_VERSION", "Migration"]
