"""Partial range repairs must unblock only the part actually re-fetched."""

from contextlib import closing
from datetime import date

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage.sources import (
    _append_source_coverage_quality_issues,
)
from baibai_engine.storage.sqlite.source_cache.coverage import record_range_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_clean_subrange_supersedes_only_that_part_of_a_quality_complaint(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            [("1301", "2026-01-05", 100.0), ("1301", "2026-01-20", 101.0)],
        )
        source = "jquants_daily_bars"

        def record(start, end, status):
            record_range_source_coverage(
                conn,
                source=source,
                operation="get_eq_bars_daily_range",
                table=source,
                date_column="traded_at",
                requested_start=start,
                requested_end=end,
                status=status,
                error="rejected record" if status != "ok" else None,
            )

        def issues(start=None, end=None):
            result = []
            _append_source_coverage_quality_issues(
                conn, result, source=source, start=start, end=end
            )
            return result

        first, middle, latter, last = (
            date(2026, 1, 1),
            date(2026, 1, 15),
            date(2026, 1, 16),
            date(2026, 1, 31),
        )
        record(first, last, "partial")
        record(first, middle, "ok")
        assert not issues(first, middle)
        assert issues(latter, last)
        assert issues(first, last)
        assert issues()

        record(latter, last, "ok")
        assert not issues(first, last)
        assert not issues()

        # A new failure supersedes successful coverage, rather than being hidden by it.
        record(first, middle, "partial")
        assert issues(first, middle)
        assert not issues(latter, last)
