"""保存済み旧runをrevision IDで読む。新規判断用のrun選択は行わない。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.screening.discovery.contracts import validate_candidate_run
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


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

    def _connect(self) -> sqlite3.Connection:
        return connect_read_only(self._path)


def _run_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> RunPublication:
    header = dict(_decode_payload(row["payload"]))
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
    analyses = tuple(dict(_decode_payload(item[0])) for item in stock_analyses)
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


def _decode_payload(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise sqlite3.DatabaseError("run store payload is not an object")
    return decoded


__all__ = ["RunPublication", "ScreeningRunReader"]
