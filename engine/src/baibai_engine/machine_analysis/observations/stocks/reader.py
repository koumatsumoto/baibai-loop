"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master_contract
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
    MasterSnapshotRead,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import masters_from_payloads
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.storage.sqlite.read import read_rows
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_eq_master_asof(sqlite_path: Path, asof: date) -> MasterSnapshotRead:
    """Read the newest master snapshot at or before ``asof``.

    Snapshot collection starts at the first locally stored date, so cohorts
    before it can never have a point-in-time snapshot. For those dates this
    falls back to the earliest stored snapshot and labels the read
    ``future_snapshot``: the approximation keeps historical cohorts measurable
    for diagnostics, while the calibration authority contract excludes every
    non-``exact_date`` read from production evidence.
    """
    if not sqlite_path.exists():
        return MasterSnapshotRead((), None, "unavailable")
    conn = connect_current(sqlite_path)
    if conn is None:
        return MasterSnapshotRead((), None, "unavailable")
    try:
        conn.execute("BEGIN")
        status = "prior_snapshot"
        snapshot = conn.execute(
            "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date <= ?",
            (asof.isoformat(),),
        ).fetchone()[0]
        if snapshot is None:
            status = "future_snapshot"
            snapshot = conn.execute(
                "SELECT MIN(snapshot_date) FROM jquants_master_snapshots"
            ).fetchone()[0]
        if snapshot is None:
            return MasterSnapshotRead((), None, "unavailable")
        rows = conn.execute(
            "SELECT record_json "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY security_code",
            (str(snapshot),),
        ).fetchall()
    finally:
        conn.close()
    snapshot_date = date.fromisoformat(str(snapshot))
    return MasterSnapshotRead(
        tuple(master_contract.masters_from_payloads([row[0] for row in rows])),
        snapshot_date,
        "exact_date" if snapshot_date == asof else status,
    )


def read_eq_master_exact(sqlite_path: Path, asof: date) -> list[SecurityMaster] | None:
    """Return an exact snapshot only when its canonical coverage also agrees."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    iso = asof.isoformat()
    try:
        conn.execute("BEGIN")
        coverage_rows = conn.execute(
            "SELECT coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = ? AND coverage_key = ?",
            (master_contract.MASTER_SOURCE, master_contract.master_coverage_key(asof)),
        ).fetchall()
        if len(coverage_rows) != 1:
            return None
        coverage_start, coverage_end, record_count, status, error = coverage_rows[0]
        if coverage_start != iso or coverage_end != iso or status != "ok" or error is not None:
            return None
        expected_count = master_contract.master_coverage_count(record_count)
        if expected_count is None:
            return None
        rows = conn.execute(
            "SELECT record_json "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY security_code",
            (iso,),
        ).fetchall()
        if len(rows) != expected_count or expected_count <= 0:
            return None
    finally:
        conn.close()
    masters = master_contract.masters_from_payloads([row[0] for row in rows])
    if (
        sum(security.is_common_stock for security in masters)
        < master_contract.MIN_COMMON_STOCK_MASTER_ROWS
    ):
        return None
    return masters


def read_master_snapshot(
    path: Path, *, as_of: date, connection: sqlite3.Connection | None = None
) -> MasterSnapshot | None:
    """Read classification on the caller's market snapshot when one is supplied."""
    statement = (
        "SELECT snapshot_date, record_json "
        "FROM jquants_master_snapshots WHERE snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
        "WHERE snapshot_date <= ?) ORDER BY security_code"
    )
    parameters = (as_of.isoformat(),)
    rows = (
        read_rows(path, statement, parameters, connector=connect_read_only)
        if connection is None
        else connection.execute(statement, parameters).fetchall()
    )
    if not rows:
        return None
    try:
        snapshot_date = date.fromisoformat(str(rows[0]["snapshot_date"]))
    except ValueError as error:
        raise JQuantsProviderError(
            f"stored master snapshot has invalid date: {rows[0]['snapshot_date']!r}"
        ) from error
    return MasterSnapshot(
        snapshot_date,
        {
            security.ticker: security
            for security in masters_from_payloads([row["record_json"] for row in rows])
        },
    )
