"""Read-only query facade for screening run-store publications."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import RUN_STORE_SCHEMA_VERSION
from .store import RunStoreAmbiguousError, decode_payload, run_store_path


@dataclass(frozen=True, slots=True)
class RunPublication:
    run_revision_id: str
    public_run_id: str
    run_date: str
    as_of_date: str
    run_at: str
    universe_size: int
    rules_ref: str | None
    payload: dict[str, Any]
    security_analyses: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReviewSetPublication:
    review_set_id: str
    run_revision_id: str
    as_of_date: str
    created_at: str
    payload: dict[str, Any]

    @property
    def as_of(self) -> str:
        return self.as_of_date


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open the run store read-only and require its current schema."""

    resolved = run_store_path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        has_tables = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version == 0 and has_tables is None:
            # An untouched file is the same no-store state as an absent path. The
            # first domain query raises OperationalError, which read_api degrades to
            # no publication; it is not an obsolete store accepted as current.
            return connection
        if version != RUN_STORE_SCHEMA_VERSION:
            raise RuntimeError(
                "screening run store schema is not current "
                f"(found {version}, expected {RUN_STORE_SCHEMA_VERSION}); rebuild it"
            )
        return connection
    except BaseException:
        connection.close()
        raise


class ScreeningRunReader:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def get_run(self, run_revision_id: str) -> RunPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM screening_run WHERE run_revision_id = ?",
                (run_revision_id,),
            ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def list_runs(self) -> list[RunPublication]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT * FROM screening_run
                ORDER BY asof_date DESC, run_at DESC, run_revision_id DESC
                """
            ).fetchall()
            return [_run_from_row(connection, row) for row in rows]

    def latest_run(self) -> RunPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT * FROM screening_run
                ORDER BY asof_date DESC, run_at DESC, run_revision_id DESC
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def latest_as_of_before(self, as_of_date: str) -> str | None:
        with closing(self._connect()) as connection:
            value = connection.execute(
                "SELECT max(asof_date) FROM screening_run WHERE asof_date < ?",
                (as_of_date,),
            ).fetchone()[0]
            return None if value is None else str(value)

    def resolve_run(
        self,
        *,
        run_revision_id: str | None = None,
        as_of_date: str | None = None,
    ) -> RunPublication | None:
        if run_revision_id is not None:
            return self.get_run(run_revision_id)
        if as_of_date is None:
            raise ValueError("run_revision_id or as_of_date is required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM screening_run WHERE asof_date = ? ORDER BY run_at, run_revision_id",
                (as_of_date,),
            ).fetchall()
            if len(rows) > 1:
                raise RunStoreAmbiguousError(
                    f"multiple run revisions for as_of_date {as_of_date}; specify run_revision_id"
                )
            return None if not rows else _run_from_row(connection, rows[0])

    def previous_run(self, *, before_as_of_date: str) -> RunPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            latest = connection.execute(
                "SELECT max(asof_date) FROM screening_run WHERE asof_date < ?",
                (before_as_of_date,),
            ).fetchone()[0]
            if latest is None:
                return None
            rows = connection.execute(
                "SELECT * FROM screening_run WHERE asof_date = ? ORDER BY run_at, run_revision_id",
                (latest,),
            ).fetchall()
            if len(rows) > 1:
                raise RunStoreAmbiguousError(
                    f"multiple previous run revisions for as_of_date {latest}; "
                    "specify one explicitly"
                )
            return _run_from_row(connection, rows[0])

    def get_review_set(self, review_set_id: str) -> ReviewSetPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT s.*, r.asof_date
                FROM review_set AS s
                JOIN screening_run AS r USING (run_revision_id)
                WHERE s.review_set_id = ?
                """,
                (review_set_id,),
            ).fetchone()
            return None if row is None else _review_set_from_row(row)

    def latest_review_set(self, *, as_of_date: str) -> ReviewSetPublication | None:
        """Return the latest published Review Set for exactly one as-of date."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT s.*, r.asof_date
                FROM review_set AS s
                JOIN screening_run AS r USING (run_revision_id)
                WHERE r.asof_date = ?
                ORDER BY julianday(s.created_at) DESC, s.review_set_id DESC
                LIMIT 1
                """,
                (as_of_date,),
            ).fetchone()
            return None if row is None else _review_set_from_row(row)

    def list_review_sets(
        self,
        *,
        run_revision_id: str | None = None,
        as_of_date: str | None = None,
    ) -> list[ReviewSetPublication]:
        clauses: list[str] = []
        parameters: list[str] = []
        if run_revision_id is not None:
            clauses.append("s.run_revision_id = ?")
            parameters.append(run_revision_id)
        if as_of_date is not None:
            clauses.append("r.asof_date = ?")
            parameters.append(as_of_date)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            # The optional clauses are fixed above and every value is bound.
            rows = connection.execute(
                """
                SELECT s.*, r.asof_date
                FROM review_set AS s
                JOIN screening_run AS r USING (run_revision_id)
                """  # nosec B608
                + where
                + " ORDER BY r.asof_date DESC, s.created_at DESC, s.review_set_id DESC",
                parameters,
            ).fetchall()
            return [_review_set_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return connect_read_only(self._path)


def _run_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> RunPublication:
    security_analyses = connection.execute(
        """
        SELECT payload FROM security_analysis
        WHERE run_revision_id = ? ORDER BY ordinal
        """,
        (row["run_revision_id"],),
    ).fetchall()
    return RunPublication(
        run_revision_id=str(row["run_revision_id"]),
        public_run_id=str(row["public_run_id"]),
        run_date=str(row["run_date"]),
        as_of_date=str(row["asof_date"]),
        run_at=str(row["run_at"]),
        universe_size=int(row["universe_size"]),
        rules_ref=None if row["rules_ref"] is None else str(row["rules_ref"]),
        payload=dict(decode_payload(row["payload"])),
        security_analyses=tuple(dict(decode_payload(item[0])) for item in security_analyses),
    )


def _review_set_from_row(row: sqlite3.Row) -> ReviewSetPublication:
    from baibai_engine.screening.discovery.review_set import validate_review_set_shape

    payload = dict(decode_payload(row["payload"]))
    validate_review_set_shape(payload)
    return ReviewSetPublication(
        review_set_id=str(row["review_set_id"]),
        run_revision_id=str(row["run_revision_id"]),
        as_of_date=str(row["asof_date"]),
        created_at=str(row["created_at"]),
        payload=payload,
    )


__all__ = [
    "ReviewSetPublication",
    "RunPublication",
    "ScreeningRunReader",
]
