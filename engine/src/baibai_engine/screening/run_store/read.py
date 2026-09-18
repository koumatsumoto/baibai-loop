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

        return self.resolve_review_set(as_of_date=as_of_date)

    def resolve_review_set(
        self,
        *,
        review_set_id: str | None = None,
        public_run_id: str | None = None,
        as_of_date: str | None = None,
        not_before: str | None = None,
    ) -> ReviewSetPublication | None:
        """Read exact identity or the daily canonical publication, without fallback.

        Date selection uses the latest publication on that day, as daily analysis
        does. not_before chooses the first eligible day; no selector chooses the last.
        A public run ID shared by revisions needs an exact Review Set ID.
        """
        if sum(x is not None for x in (review_set_id, public_run_id, as_of_date, not_before)) > 1:
            raise ValueError("at most one Review Set selector is allowed")
        if review_set_id is not None:
            return self.get_review_set(review_set_id)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            if public_run_id is not None:
                runs = connection.execute(
                    "SELECT run_revision_id FROM screening_run WHERE public_run_id = ?",
                    (public_run_id,),
                ).fetchall()
                if len(runs) > 1:
                    raise RunStoreAmbiguousError("public run ID has multiple revisions")
                if not runs:
                    return None
                clause, parameters = "r.run_revision_id = ?", (runs[0][0],)
            elif as_of_date is not None:
                clause, parameters = "r.asof_date = ?", (as_of_date,)
            else:
                aggregate = "min" if not_before is not None else "max"
                day = connection.execute(
                    f"SELECT {aggregate}(r.asof_date) FROM review_set s "  # nosec B608
                    "JOIN screening_run r USING (run_revision_id) "
                    "WHERE (? IS NULL OR r.asof_date >= ?)",
                    (not_before, not_before),
                ).fetchone()[0]
                if day is None:
                    return None
                clause, parameters = "r.asof_date = ?", (day,)
            row = connection.execute(
                "SELECT s.*, r.asof_date FROM review_set s "
                "JOIN screening_run r USING (run_revision_id) WHERE "  # nosec B608
                + clause
                + " ORDER BY julianday(s.created_at) DESC, s.review_set_id DESC LIMIT 1",
                parameters,
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


def stored_run_page(
    connection: sqlite3.Connection,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None,
    limit: int,
    full: bool = False,
) -> list[dict[str, Any]]:
    """Bounded headers/analyses/publications without loading every child analysis."""
    from baibai_engine.foundation.sqlite_pages import select_page

    if kind == "security_analysis":
        parent = filters["run_revision_id"]
        if (
            connection.execute(
                "SELECT 1 FROM screening_run WHERE run_revision_id = ?", (parent,)
            ).fetchone()
            is None
        ):
            raise FileNotFoundError("run unavailable")
        rows = select_page(
            connection, table=kind, order=("ordinal",), equal=filters, after=after, limit=limit
        )
    elif kind == "screening_run":
        equal = {key: filters[key] for key in ("run_revision_id",) if key in filters}
        if "as_of_date" in filters:
            equal["asof_date"] = filters["as_of_date"]
        ranges = [
            ("asof_date", op, filters[key])
            for key, op in (("from", ">="), ("to", "<="))
            if key in filters
        ]
        rows = select_page(
            connection,
            table=kind,
            columns="*"
            if full
            else "run_revision_id, public_run_id, run_date, asof_date, "
            "run_at, universe_size, rules_ref, created_at",
            order=("asof_date", "run_at", "run_revision_id"),
            equal=equal,
            ranges=ranges,
            after=after,
            limit=limit,
        )
    elif kind == "review_set":
        equal = {
            f"s.{key}": filters[key]
            for key in ("run_revision_id", "review_set_id")
            if key in filters
        }
        if (
            "run_revision_id" in filters
            and connection.execute(
                "SELECT 1 FROM screening_run WHERE run_revision_id = ?",
                (filters["run_revision_id"],),
            ).fetchone()
            is None
        ):
            raise FileNotFoundError("run unavailable")
        rows = select_page(
            connection,
            table="review_set s JOIN screening_run r USING (run_revision_id)",
            columns=(
                "s.*, r.asof_date, julianday(s.created_at) AS page_time"
                if full
                else "s.review_set_id, s.run_revision_id, s.created_at, r.asof_date, "
                "julianday(s.created_at) AS page_time"
            ),
            order=("r.asof_date", "julianday(s.created_at)", "s.review_set_id"),
            equal=equal,
            ranges=[
                ("r.asof_date", op, filters[key])
                for key, op in (("from", ">="), ("to", "<="))
                if key in filters
            ],
            after=after,
            limit=limit,
        )
    else:
        raise ValueError("unknown run row kind")
    for row in rows:
        if "payload" in row:
            row["payload"] = dict(decode_payload(row["payload"]))
    return rows
