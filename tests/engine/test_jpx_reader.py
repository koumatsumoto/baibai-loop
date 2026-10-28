from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    index_calendar_announcements,
    next_earnings_dates,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import (
    JPXReader,
    read_jpx_earnings_calendar_snapshot,
    read_next_earnings_dates,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
)


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


@pytest.mark.parametrize("store", ["absent-file", "empty-file", "unwritten-header"])
@pytest.mark.parametrize("allow_stale", [False, True])
def test_unwritten_schedule_is_absent_for_optional_and_required_readers(
    tmp_path: Path, store: str, allow_stale: bool
) -> None:
    path = tmp_path / "market.sqlite"
    asof = date(2026, 5, 8)
    if store == "empty-file":
        sqlite3.connect(path).close()
    elif store == "unwritten-header":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
        assert path.stat().st_size > 0
    before = path.read_bytes() if path.exists() else None
    assert read_jpx_earnings_calendar_snapshot(path, asof, allow_stale=allow_stale) is None
    assert read_next_earnings_dates(path, asof) is None
    with pytest.raises(JPXProviderError, match="stored JPX earnings calendar is unavailable"):
        JPXReader(path, allow_stale_snapshot=allow_stale).get_earnings_calendar_snapshot(asof)
    assert (path.read_bytes() if path.exists() else None) == before


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
        read_jpx_earnings_calendar_snapshot(calendar_path, date(2026, 5, 8))
    with pytest.raises(JPXProviderError, match=message):
        read_next_earnings_dates(calendar_path, date(2026, 5, 8))
    with pytest.raises(JPXProviderError, match=message):
        JPXReader(calendar_path).get_earnings_calendar_snapshot(date(2026, 5, 8))
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


@pytest.mark.parametrize("store", ["unversioned-table", "versioned-empty", "obsolete-schema"])
def test_existing_noncurrent_schema_is_not_unknown(tmp_path: Path, store: str) -> None:
    path = tmp_path / "market.sqlite"
    if store == "obsolete-schema":
        open_connection(path).close()
    with closing(sqlite3.connect(path)) as connection, connection:
        if store == "unversioned-table":
            connection.execute("CREATE TABLE another_table(value TEXT)")
        else:
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
    before = path.read_bytes()
    with pytest.raises(SQLiteSchemaError):
        read_jpx_earnings_calendar_snapshot(path, date(2026, 5, 8))
    with pytest.raises(SQLiteSchemaError):
        read_next_earnings_dates(path, date(2026, 5, 8))
    with pytest.raises(SQLiteSchemaError):
        JPXReader(path).get_earnings_calendar_snapshot(date(2026, 5, 8))
    assert path.read_bytes() == before


def test_corrupt_file_is_not_unknown(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    before = b"not a SQLite database"
    path.write_bytes(before)
    with pytest.raises(sqlite3.DatabaseError):
        read_jpx_earnings_calendar_snapshot(path, date(2026, 5, 8))
    with pytest.raises(sqlite3.DatabaseError):
        read_next_earnings_dates(path, date(2026, 5, 8))
    with pytest.raises(sqlite3.DatabaseError):
        JPXReader(path).get_earnings_calendar_snapshot(date(2026, 5, 8))
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["earnings", "regulations"])
def test_jpx_snapshot_keeps_one_generation_across_concurrent_refresh(
    calendar_path, monkeypatch, kind
):
    import baibai_engine.machine_analysis.observations.disclosures.jpx_reader as jpx_reader

    path = calendar_path
    asof = date(2026, 5, 8)
    store_jpx_regulations(
        path,
        asof,
        flags_by_ticker={"1301": ("old-source",)},
        source_names=("old-source",),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    original_connect = jpx_reader.connect_current
    committed = []

    def refresh():
        assert not committed
        if kind == "earnings":
            store_jpx_earnings_calendar_snapshot(
                path,
                JPXEarningsCalendarSnapshot(
                    entries=(JPXEarningsCalendarEntry("9999", date(2026, 6, 2)),),
                    source_urls=(),
                    raw_record_count=1,
                    excluded_record_count=0,
                    superseded_record_count=0,
                ),
                fetched_at_utc="2026-05-08T01:00:00+00:00",
            )
        else:
            store_jpx_regulations(
                path,
                asof,
                flags_by_ticker={"9999": ("new-source",)},
                source_names=("new-source",),
                fetched_at_utc="2026-05-08T01:00:00+00:00",
            )
        committed.append(True)

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            result = self.cursor.fetchone()
            refresh()
            return result

        def fetchall(self):
            result = self.cursor.fetchall()
            refresh()
            return result

    class Connection:
        def __init__(self, database):
            self.connection = original_connect(database)

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            trigger = "FROM source_coverage" if kind == "earnings" else "FROM jpx_regulation_flags"
            return Cursor(cursor) if trigger in sql else cursor

        def close(self):
            self.connection.close()

    with monkeypatch.context() as scoped:
        scoped.setattr(jpx_reader, "connect_current", Connection)
        if kind == "earnings":
            snapshot = read_jpx_earnings_calendar_snapshot(path, asof)
            assert [entry.ticker for entry in snapshot.entries] == ["1301", "130A", "2914"]
        else:
            snapshot = jpx_reader.read_jpx_regulations(path, asof)
            assert snapshot.flags_by_ticker == {"1301": ("old-source",)}
            assert snapshot.source_names == ("old-source",)
    assert committed == [True]
    if kind == "earnings":
        assert read_next_earnings_dates(path, asof) == {"9999": date(2026, 6, 2)}
    else:
        snapshot = jpx_reader.read_jpx_regulations(path, asof)
        assert snapshot.flags_by_ticker == {"9999": ("new-source",)}
        assert snapshot.source_names == ("new-source",)
