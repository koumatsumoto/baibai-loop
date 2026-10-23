from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    index_calendar_announcements,
    next_earnings_dates,
)
from baibai_engine.market.jpx_reader import JPXReader, read_next_earnings_dates
from baibai_engine.storage.observations.ingest.jpx import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection


@pytest.fixture
def calendar_path(tmp_path: Path) -> Path:
    path = tmp_path / "market.sqlite"
    store_jpx_earnings_calendar_snapshot(
        path,
        JPXEarningsCalendarSnapshot(
            entries=(
                JPXEarningsCalendarEntry("1301", date(2026, 5, 7)),
                JPXEarningsCalendarEntry("130A", date(2026, 5, 8)),
                JPXEarningsCalendarEntry("2914", date(2026, 6, 1)),
            ),
            source_urls=(),
            raw_record_count=3,
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )
    return path


def test_reader_returns_saved_snapshots_without_acquisition(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    asof = date(2026, 5, 8)
    store_jpx_regulations(
        sqlite_path,
        asof,
        flags_by_ticker={"1301": ("取引停止",)},
        source_names=("取引停止",),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )
    store_jpx_earnings_calendar_snapshot(
        sqlite_path,
        JPXEarningsCalendarSnapshot(
            entries=(JPXEarningsCalendarEntry("1301", date(2026, 6, 1)),),
            source_urls=("https://www.jpx.co.jp/test/kessan.xlsx",),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )

    with patch("requests.Session", side_effect=AssertionError("HTTP session constructed")):
        reader = JPXReader(sqlite_path)
        assert reader.get_regulation_snapshot(asof).flags_by_ticker == {"1301": ("取引停止",)}
        assert reader.get_earnings_calendar_snapshot(asof).entries == (
            JPXEarningsCalendarEntry("1301", date(2026, 6, 1)),
        )
        with pytest.raises(JPXProviderError, match="stored JPX earnings calendar is unavailable"):
            reader.get_earnings_calendar_snapshot(date(2026, 5, 22))
        assert (
            JPXReader(sqlite_path, allow_stale_snapshot=True)
            .get_earnings_calendar_snapshot(date(2026, 5, 22))
            .entries[0]
            .ticker
            == "1301"
        )


def test_reader_reports_missing_saved_snapshots(tmp_path: Path) -> None:
    reader = JPXReader(tmp_path / "missing.sqlite")
    with pytest.raises(JPXProviderError, match="stored JPX regulation snapshot is unavailable"):
        reader.get_regulation_snapshot(date(2026, 5, 8))
    with pytest.raises(JPXProviderError, match="stored JPX earnings calendar is unavailable"):
        reader.get_earnings_calendar_snapshot(date(2026, 5, 8))


def test_next_dates_include_today_and_keep_alphanumeric_tickers(calendar_path: Path) -> None:
    before = calendar_path.read_bytes()
    with patch("requests.Session", side_effect=AssertionError("HTTP session constructed")):
        assert read_next_earnings_dates(calendar_path, date(2026, 5, 8)) == {
            "130A": date(2026, 5, 8),
            "2914": date(2026, 6, 1),
        }
    assert calendar_path.read_bytes() == before


def test_pure_selection_does_not_turn_conflicting_dates_into_an_earliest_date() -> None:
    entries = (
        JPXEarningsCalendarEntry("1301", date(2026, 5, 7)),
        JPXEarningsCalendarEntry("1301", date(2026, 5, 13)),
    )
    with pytest.raises(ValueError, match="conflicting rows for 1301"):
        next_earnings_dates(entries, date(2026, 5, 8))
    assert index_calendar_announcements(entries[:1]) == {"1301": date(2026, 5, 7)}
    assert next_earnings_dates(entries[:1], date(2026, 5, 8)) == {}


def test_optional_dates_require_current_successful_coverage(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    assert read_next_earnings_dates(path, date(2026, 5, 8)) is None
    assert not path.exists()
    with closing(open_connection(path)) as connection, connection:
        connection.execute("INSERT INTO jpx_earnings_calendar VALUES ('2026-06-01', '2914')")
    assert read_next_earnings_dates(path, date(2026, 5, 8)) is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE source_coverage SET coverage_key = 'legacy'",
        "UPDATE source_coverage SET status = 'partial'",
        "UPDATE source_coverage SET fetched_at_utc = '2026-04-01T00:00:00Z'",
    ],
)
def test_unavailable_schedule_is_unknown(calendar_path: Path, change: str) -> None:
    with closing(sqlite3.connect(calendar_path)) as connection, connection:
        connection.execute(change)
    assert read_next_earnings_dates(calendar_path, date(2026, 5, 8)) is None


def test_optional_dates_keep_existing_freshness_and_schedule_end_rules(calendar_path: Path) -> None:
    assert read_next_earnings_dates(calendar_path, date(2026, 5, 19)) == {"2914": date(2026, 6, 1)}
    assert read_next_earnings_dates(calendar_path, date(2026, 5, 20)) is None
    assert read_next_earnings_dates(calendar_path, date(2026, 6, 2)) is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("UPDATE source_coverage SET record_count = 2", "row count mismatch"),
        ("UPDATE source_coverage SET record_count = 'broken'", "corrupt.*coverage"),
        ("UPDATE source_coverage SET coverage_start = '2026-05-01'", "date range mismatch"),
        ("UPDATE source_coverage SET coverage_end = NULL", "corrupt.*coverage"),
        ("UPDATE source_coverage SET fetched_at_utc = 'invalid'", "corrupt.*coverage"),
        (
            (
                "UPDATE jpx_earnings_calendar SET announcement_date = '2026-99-99' "
                "WHERE ticker = '130A'"
            ),
            "corrupt.*snapshot",
        ),
    ],
)
def test_corrupt_successful_snapshot_is_an_error(
    calendar_path: Path, change: str, message: str
) -> None:
    with closing(sqlite3.connect(calendar_path)) as connection, connection:
        connection.execute(change)
    before = calendar_path.read_bytes()
    with pytest.raises(JPXProviderError, match=message):
        read_next_earnings_dates(calendar_path, date(2026, 5, 8))
    assert calendar_path.read_bytes() == before


def test_stored_conflict_is_rejected_before_filtering_past_dates(calendar_path: Path) -> None:
    with closing(sqlite3.connect(calendar_path)) as connection, connection:
        connection.execute("INSERT INTO jpx_earnings_calendar VALUES ('2026-05-13', '1301')")
        connection.execute("UPDATE source_coverage SET record_count = 4")
    with pytest.raises(JPXProviderError, match="conflicting rows for 1301"):
        read_next_earnings_dates(calendar_path, date(2026, 5, 8))


def test_legacy_coverage_does_not_override_current_schedule(calendar_path: Path) -> None:
    with closing(sqlite3.connect(calendar_path)) as connection, connection:
        connection.execute(
            "INSERT INTO source_coverage(source, coverage_key, fetched_at_utc, record_count, status) "
            "VALUES ('jpx_earnings_calendar', 'legacy', 'invalid', 99, 'ok')"
        )
    assert read_next_earnings_dates(calendar_path, date(2026, 5, 8)) == {
        "130A": date(2026, 5, 8),
        "2914": date(2026, 6, 1),
    }


def test_existing_partial_schema_is_not_unknown(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE another_table(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(SQLiteSchemaError):
        read_next_earnings_dates(path, date(2026, 5, 8))
    assert path.read_bytes() == before
