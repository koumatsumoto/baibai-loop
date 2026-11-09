"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master_contract
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import masters_from_payloads
from baibai_engine.storage.sqlite.read import read_rows
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def read_master_snapshot(
    path: Path, *, as_of: date, connection: sqlite3.Connection | None = None
) -> MasterSnapshot | None:
    """Read classification on the caller's market snapshot when one is supplied."""
    statement = (
        "SELECT snapshot_date, record_json "
        "FROM jquants_master_snapshots WHERE snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
        "WHERE snapshot_date <= ?) AND snapshot_date > COALESCE(("
        "SELECT MAX(coverage_start) FROM source_coverage WHERE source=? "
        "AND coverage_start=coverage_end AND coverage_start<=? "
        "AND status='unavailable' AND error LIKE ?), '') ORDER BY security_code"
    )
    parameters = (
        as_of.isoformat(),
        master_contract.MASTER_SOURCE,
        as_of.isoformat(),
        master_contract.MASTER_VALIDATION_ERROR_PREFIX + "%",
    )
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
