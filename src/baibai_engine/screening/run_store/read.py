"""Read-only query facade for screening run-store publications."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    candidates: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SelectionPublication:
    selection_id: str
    run_revision_id: str
    as_of_date: str
    profile: str
    macro_context_id: str | None
    publication_kind: str
    created_at: str
    source_selection_id: str | None
    payload: dict[str, Any]
    entries: tuple[dict[str, Any], ...]

    @property
    def as_of(self) -> str:
        return self.as_of_date


class ScreeningRunReader:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def get_run(self, run_revision_id: str) -> RunPublication | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM screening_run WHERE run_revision_id = ?",
                (run_revision_id,),
            ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def list_runs(self) -> list[RunPublication]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM screening_run
                ORDER BY asof_date DESC, run_at DESC, run_revision_id DESC
                """
            ).fetchall()
            return [_run_from_row(connection, row) for row in rows]

    def latest_run(self) -> RunPublication | None:
        with closing(self._connect()) as connection:
            latest = connection.execute("SELECT max(asof_date) FROM screening_run").fetchone()[0]
            if latest is None:
                return None
            rows = connection.execute(
                "SELECT * FROM screening_run WHERE asof_date = ? ORDER BY run_at, run_revision_id",
                (latest,),
            ).fetchall()
            if len(rows) > 1:
                raise RunStoreAmbiguousError(
                    f"multiple latest run revisions for as_of_date {latest}; "
                    "specify run_revision_id"
                )
            return _run_from_row(connection, rows[0])

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

    def get_selection(self, selection_id: str) -> SelectionPublication | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT s.*, r.asof_date
                FROM screening_selection AS s
                JOIN screening_run AS r USING (run_revision_id)
                WHERE s.selection_id = ?
                """,
                (selection_id,),
            ).fetchone()
            return None if row is None else _selection_from_row(connection, row)

    def list_selections(
        self,
        *,
        run_revision_id: str | None = None,
        as_of_date: str | None = None,
        profile: str | None = None,
    ) -> list[SelectionPublication]:
        clauses: list[str] = []
        parameters: list[str] = []
        if run_revision_id is not None:
            clauses.append("s.run_revision_id = ?")
            parameters.append(run_revision_id)
        if as_of_date is not None:
            clauses.append("r.asof_date = ?")
            parameters.append(as_of_date)
        if profile is not None:
            clauses.append("s.profile = ?")
            parameters.append(profile)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT s.*, r.asof_date
                FROM screening_selection AS s
                JOIN screening_run AS r USING (run_revision_id)
                """
                + where
                + " ORDER BY r.asof_date DESC, s.created_at DESC, s.selection_id DESC",
                parameters,
            ).fetchall()
            return [_selection_from_row(connection, row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        path = run_store_path(self._path).resolve()
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _run_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> RunPublication:
    candidates = connection.execute(
        """
        SELECT payload FROM screening_candidate
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
        candidates=tuple(dict(decode_payload(item[0])) for item in candidates),
    )


def _selection_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> SelectionPublication:
    entries = connection.execute(
        "SELECT payload FROM selection_entry WHERE selection_id = ? ORDER BY ordinal",
        (row["selection_id"],),
    ).fetchall()
    return SelectionPublication(
        selection_id=str(row["selection_id"]),
        run_revision_id=str(row["run_revision_id"]),
        as_of_date=str(row["asof_date"]),
        profile=str(row["profile"]),
        macro_context_id=(
            None if row["macro_context_id"] is None else str(row["macro_context_id"])
        ),
        publication_kind=str(row["publication_kind"]),
        created_at=str(row["created_at"]),
        source_selection_id=(
            None if row["source_selection_id"] is None else str(row["source_selection_id"])
        ),
        payload=dict(decode_payload(row["payload"])),
        entries=tuple(dict(decode_payload(item[0])) for item in entries),
    )


__all__ = [
    "RunPublication",
    "ScreeningRunReader",
    "SelectionPublication",
]
