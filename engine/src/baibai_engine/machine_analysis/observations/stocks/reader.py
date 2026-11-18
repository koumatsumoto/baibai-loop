"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master_contract
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import master_from_payload
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.sqlite.read import read_rows
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def read_master_snapshot(
    path: Path,
    *,
    as_of: date,
    tickers: Sequence[str] | None = None,
    connection: sqlite3.Connection | None = None,
) -> MasterSnapshot | None:
    """Choose the eligible market-wide date before narrowing to requested primary issue lines."""
    # LIMIT 1 preserves the date index seek even with the coverage cutoff below.
    statement = (
        "WITH latest(day) AS (SELECT snapshot_date FROM jquants_master_snapshots "
        "WHERE snapshot_date<=? ORDER BY snapshot_date DESC LIMIT 1) "
        "SELECT day AS snapshot_date, m.security_code, m.record_json FROM latest "
        "LEFT JOIN jquants_master_snapshots m ON m.snapshot_date=day"
    )
    parameters: tuple[object, ...] = (as_of.isoformat(),)
    if tickers is not None:
        # The index uses the five-character provider code. A non-zero issue suffix
        # cannot supply another issue's name or classification. Keep a single binding.
        codes = sorted({f"{normalize_ticker(ticker)}0" for ticker in tickers})
        statement += " AND m.security_code IN (SELECT value FROM json_each(?))"
        parameters += (json.dumps(codes),)
    statement += (
        " WHERE day>COALESCE((SELECT MAX(coverage_start) FROM source_coverage "
        "WHERE source=? AND coverage_start=coverage_end AND coverage_start<=? "
        "AND status='unavailable' AND error LIKE ?), '') ORDER BY m.security_code"
    )
    parameters += (
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
    securities = {}
    for row in rows:
        if row["security_code"] is None:
            continue  # A valid snapshot can contain none of the requested tickers.
        security = master_from_payload(
            row["record_json"], expected_code=row["security_code"] if tickers is not None else None
        )
        if security is not None:
            securities[security.ticker] = security
    return MasterSnapshot(
        snapshot_date, dict(sorted(securities.items())) if tickers is None else securities
    )
