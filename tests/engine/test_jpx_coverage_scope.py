"""Unrelated JPX snapshots must not invalidate the requested run."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.test_sqlite_coverage import (
    _COMMON_COVERAGE_ASOF,
    _complete_coverage_database,
    _verify_screening_sqlite_coverage,
)


@pytest.mark.parametrize("offset", [-7, 7])
@pytest.mark.parametrize("status", ["partial", "ok"])
def test_unrelated_regulation_rows_and_receipt_counts_do_not_block_current_run(offset, status):
    asof = _COMMON_COVERAGE_ASOF
    other = (asof + timedelta(days=offset)).isoformat()
    with _complete_coverage_database() as path:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO jpx_regulation_flags VALUES (?, 'test-source', '1301', 'test-flag', NULL)",
                (other,),
            )
            connection.execute(
                "INSERT INTO source_coverage VALUES ('jpx_regulation_flags', ?, ?, ?, ?, ?, ?, NULL)",
                (other, other, other, other + "T00:00:00Z", 99 if status == "ok" else 1, status),
            )
        assert _verify_screening_sqlite_coverage(path, asof) == ()


def test_requested_regulation_snapshot_still_rejects_its_own_count_mismatch():
    asof = _COMMON_COVERAGE_ASOF
    with _complete_coverage_database() as path:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO jpx_regulation_flags VALUES (?, 'test-source', '1301', 'test-flag', NULL)",
                (asof.isoformat(),),
            )
        issues = _verify_screening_sqlite_coverage(path, asof)
        assert any(
            issue.source == "jpx_regulation_flags" and "row count" in issue.reason
            for issue in issues
        )
