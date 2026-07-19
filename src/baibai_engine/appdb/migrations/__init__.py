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
    Migration(
        version=3,
        statements=(
            """
            CREATE TABLE macro_context (
                context_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                published_at TEXT NOT NULL,
                supersedes_id TEXT REFERENCES macro_context(context_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (valid_until >= as_of)
            ) STRICT
            """,
            """
            CREATE TABLE macro_context_head (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                context_id TEXT NOT NULL REFERENCES macro_context(context_id)
            ) STRICT
            """,
            "CREATE INDEX macro_context_asof_idx ON macro_context(as_of, published_at, context_id)",
        ),
    ),
    Migration(
        version=4,
        statements=(
            """
            CREATE TABLE reviewed_shortlist (
                shortlist_id TEXT PRIMARY KEY,
                selection_id TEXT NOT NULL,
                run_revision_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                published_at TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            "CREATE INDEX reviewed_shortlist_asof_idx ON reviewed_shortlist(as_of, published_at)",
            "CREATE INDEX reviewed_shortlist_selection_idx ON reviewed_shortlist(selection_id)",
        ),
    ),
    Migration(
        version=5,
        statements=(
            """
            CREATE TABLE research_packet (
                packet_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                as_of TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                published_at TEXT NOT NULL,
                supersedes_id TEXT REFERENCES research_packet(packet_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (supersedes_id IS NULL OR supersedes_id <> packet_id)
            ) STRICT
            """,
            "CREATE INDEX research_packet_ticker_idx "
            "ON research_packet(ticker, as_of, published_at, packet_id)",
            """
            CREATE TABLE research_review (
                review_id TEXT PRIMARY KEY,
                packet_id TEXT NOT NULL REFERENCES research_packet(packet_id),
                reviewed_at TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT
            """,
            "CREATE INDEX research_review_packet_idx "
            "ON research_review(packet_id, reviewed_at, review_id)",
            """
            CREATE TABLE holding_review (
                holding_review_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                as_of TEXT NOT NULL,
                packet_id TEXT NOT NULL REFERENCES research_packet(packet_id),
                candidate_packet_id TEXT REFERENCES research_packet(packet_id),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                CHECK (candidate_packet_id IS NULL OR candidate_packet_id <> packet_id)
            ) STRICT
            """,
            "CREATE INDEX holding_review_ticker_idx "
            "ON holding_review(ticker, as_of, holding_review_id)",
            "CREATE INDEX holding_review_packet_idx ON holding_review(packet_id)",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version

__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration"]
