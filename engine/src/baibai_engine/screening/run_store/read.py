"""Read-only query facade for screening run-store publications."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.screening.discovery.contracts import validate_candidate_run

from .schema import RUN_STORE_SCHEMA_VERSION
from .store import (
    RunStoreAmbiguousError,
    decode_payload,
    register_run_clock,
    run_clock,
    run_store_path,
)


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
    stock_analyses: tuple[dict[str, Any], ...]


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open the run store read-only and require its current schema."""

    resolved = run_store_path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    register_run_clock(connection)
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
                ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC
                """
            ).fetchall()
            return [_run_from_row(connection, row) for row in rows]

    def latest_run(self) -> RunPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT * FROM screening_run
                ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def latest_run_on(self, *, as_of: date) -> RunPublication | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM screening_run WHERE asof_date = ? "
                "ORDER BY run_clock(run_at) DESC, run_revision_id DESC LIMIT 1",
                (as_of.isoformat(),),
            ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def analysis_run(
        self, *, as_of: date, known_at: datetime, run_revision_id: str | None = None
    ) -> RunPublication | None:
        """Select one analysis within Research's date and known-time boundaries."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValueError("known_at must be timezone-aware")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            if run_revision_id is not None:
                row = connection.execute(
                    "SELECT * FROM screening_run WHERE run_revision_id = ?", (run_revision_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown run_revision_id: {run_revision_id}")
                if (
                    row["asof_date"] > as_of.isoformat()
                    or datetime.fromisoformat(row["run_at"]) > known_at
                ):
                    raise ValueError("explicit analysis run is after the Research cutoff")
            else:
                row = connection.execute(
                    "SELECT * FROM screening_run WHERE asof_date <= ? "
                    "AND run_clock(run_at) <= ? "
                    "ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC LIMIT 1",
                    (as_of.isoformat(), run_clock(known_at.isoformat())),
                ).fetchone()
            return None if row is None else _run_from_row(connection, row)

    def valuation_run(
        self, *, as_of: date, run_revision_id: str | None = None
    ) -> RunPublication | None:
        """Resolve a retained analysis run without consulting candidate publications."""
        if run_revision_id is not None:
            run = self.get_run(run_revision_id)
            if run is None:
                raise ValueError(f"unknown run_revision_id: {run_revision_id}")
            if run.as_of_date > as_of.isoformat():
                raise ValueError(f"screening run as-of is after cutoff: {run.as_of_date} > {as_of}")
            return run
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM screening_run WHERE asof_date <= ? "
                "ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC LIMIT 1",
                (as_of.isoformat(),),
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
                "SELECT * FROM screening_run WHERE asof_date = ? "
                "ORDER BY run_clock(run_at), run_revision_id",
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
                "SELECT * FROM screening_run WHERE asof_date = ? "
                "ORDER BY run_clock(run_at), run_revision_id",
                (latest,),
            ).fetchall()
            if len(rows) > 1:
                raise RunStoreAmbiguousError(
                    f"multiple previous run revisions for as_of_date {latest}; "
                    "specify one explicitly"
                )
            return _run_from_row(connection, rows[0])

    def _connect(self) -> sqlite3.Connection:
        return connect_read_only(self._path)


def _run_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> RunPublication:
    stock_analyses = connection.execute(
        """
        SELECT payload FROM stock_analysis
        WHERE run_revision_id = ? ORDER BY ordinal
        """,
        (row["run_revision_id"],),
    ).fetchall()
    header = dict(decode_payload(row["payload"]))
    analyses = tuple(dict(decode_payload(item[0])) for item in stock_analyses)
    validate_candidate_run(header, analyses)
    return RunPublication(
        run_revision_id=str(row["run_revision_id"]),
        public_run_id=str(row["public_run_id"]),
        run_date=str(row["run_date"]),
        as_of_date=str(row["asof_date"]),
        run_at=str(row["run_at"]),
        universe_size=int(row["universe_size"]),
        rules_ref=None if row["rules_ref"] is None else str(row["rules_ref"]),
        payload=header,
        stock_analyses=analyses,
    )


__all__ = ["RunPublication", "ScreeningRunReader"]


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

    if kind == "stock_analysis":
        parent = filters["run_revision_id"]
        _require_candidate_shape(connection, str(parent))
        equal = {key: value for key, value in filters.items() if key != "candidates_only"}
        ranges: list[tuple[str, str, object]] = (
            [("json_array_length(json_extract(payload, '$.candidate.reasons'))", ">", 0)]
            if filters.get("candidates_only")
            else []
        )
        rows = select_page(
            connection,
            table=kind,
            order=("ordinal",),
            equal=equal,
            ranges=ranges,
            after=after,
            limit=limit,
        )
    elif kind == "screening_run":
        register_run_clock(connection)
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
            columns="*, run_clock(run_at) AS page_time"
            if full
            else "run_revision_id, public_run_id, run_date, asof_date, "
            "run_at, universe_size, rules_ref, created_at, run_clock(run_at) AS page_time",
            order=("asof_date", "run_clock(run_at)", "run_revision_id"),
            equal=equal,
            ranges=ranges,
            after=after,
            limit=limit,
        )
    else:
        raise ValueError("unknown run row kind")
    for row in rows:
        if "payload" in row:
            row["payload"] = dict(decode_payload(row["payload"]))
    return rows


def _require_candidate_shape(connection: sqlite3.Connection, run_revision_id: str) -> None:
    """Use the domain contract before filtering, without loading full analysis payloads."""
    row = connection.execute(
        "SELECT payload FROM screening_run WHERE run_revision_id = ?", (run_revision_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError("run unavailable")
    contexts = connection.execute(
        """
        SELECT json_object(
            'ticker', json_extract(payload, '$.ticker'),
            'candidate', json_extract(payload, '$.candidate')
        ) FROM stock_analysis WHERE run_revision_id = ? ORDER BY ordinal
        """,
        (run_revision_id,),
    )
    validate_candidate_run(
        decode_payload(row[0]), [decode_payload(context[0]) for context in contexts]
    )
