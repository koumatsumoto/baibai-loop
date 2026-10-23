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
            else:
                row = connection.execute(
                    "SELECT * FROM screening_run WHERE asof_date <= ? "
                    "AND run_clock(run_at) <= ? "
                    "ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC LIMIT 1",
                    (as_of.isoformat(), run_clock(known_at.isoformat())),
                ).fetchone()
            run = None if row is None else _run_from_row(connection, row)
            if (
                run_revision_id is not None
                and run is not None
                and (
                    date.fromisoformat(run.as_of_date) > as_of
                    or datetime.fromisoformat(run.run_at) > known_at
                )
            ):
                raise ValueError("explicit analysis run is after the Research cutoff")
            return run

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

    def _connect(self) -> sqlite3.Connection:
        return connect_read_only(self._path)


def _run_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> RunPublication:
    header = dict(decode_payload(row["payload"]))
    as_of_date = str(row["asof_date"])
    run_at = str(row["run_at"])
    date.fromisoformat(as_of_date)
    if datetime.fromisoformat(run_at).utcoffset() is None:
        raise ValueError("Screening Run run_at must be timezone-aware")
    if header.get("asof_date") != as_of_date or header.get("run_at") != run_at:
        raise ValueError("Screening Run header differs from its canonical identity")
    stock_analyses = connection.execute(
        """
        SELECT payload FROM stock_analysis
        WHERE run_revision_id = ? ORDER BY ordinal
        """,
        (row["run_revision_id"],),
    ).fetchall()
    analyses = tuple(dict(decode_payload(item[0])) for item in stock_analyses)
    validate_candidate_run(header, analyses)
    return RunPublication(
        run_revision_id=str(row["run_revision_id"]),
        public_run_id=str(row["public_run_id"]),
        run_date=str(row["run_date"]),
        as_of_date=as_of_date,
        run_at=run_at,
        universe_size=int(row["universe_size"]),
        rules_ref=None if row["rules_ref"] is None else str(row["rules_ref"]),
        payload=header,
        stock_analyses=analyses,
    )


__all__ = ["RunPublication", "ScreeningRunReader"]
