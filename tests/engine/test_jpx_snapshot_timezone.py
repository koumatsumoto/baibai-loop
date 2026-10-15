from contextlib import closing
from datetime import date

import pytest

from baibai_engine.screening.sqlite_coverage.jpx import (
    _append_jpx_earnings_calendar_issues,
    _append_jpx_freshness_issues,
)
from baibai_engine.storage.observations.coverage import record_source_coverage
from baibai_engine.storage.observations.reader import read_jpx_earnings_calendar_snapshot
from baibai_engine.storage.observations.schema import open_connection


@pytest.mark.parametrize("allow_stale", [False, True])
@pytest.mark.parametrize(
    ("fetched_at", "readable"),
    [("2026-10-14T12:00:00", False), ("2026-10-14T12:00:00+09:00", True)],
)
def test_calendar_snapshot_requires_explicit_fetch_timezone(
    tmp_path, allow_stale, fetched_at, readable
):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection, connection:
        connection.execute(
            "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
            ("2026-10-15", "7203"),
        )
        record_source_coverage(
            connection,
            source="jpx_earnings_calendar",
            coverage_key="get_earnings_calendar_snapshot:current",
            coverage_start="2026-10-15",
            coverage_end="2026-10-15",
            fetched_at_utc=fetched_at,
            record_count=1,
        )
    snapshot = read_jpx_earnings_calendar_snapshot(
        path, date(2026, 10, 14), allow_stale=allow_stale
    )
    assert (snapshot is not None) is readable
    with closing(open_connection(path)) as connection:
        issues = []
        _append_jpx_earnings_calendar_issues(
            connection, issues, asof_date=date(2026, 10, 14), allow_stale_jpx=allow_stale
        )
    assert bool(issues) is not readable


@pytest.mark.parametrize("allow_stale", [False, True])
def test_regulation_preflight_rejects_naive_fetch_timestamp(tmp_path, allow_stale):
    with closing(open_connection(tmp_path / "market.sqlite")) as connection, connection:
        connection.execute(
            "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
            "VALUES (?, ?, ?)",
            ("2026-10-14", "取引停止", "2026-10-14T12:00:00"),
        )
        issues = []
        _append_jpx_freshness_issues(
            connection, issues, asof_date=date(2026, 10, 14), allow_stale_jpx=allow_stale
        )
    assert len(issues) == 1
    assert "invalid fetched_at_utc" in issues[0].reason
