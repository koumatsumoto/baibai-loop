from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.macro.indicators.db import initialize_database, record_provider_run
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.read_api.system import (
    application_store_stats,
    never_attempted_series,
    provider_failure_streaks,
    store_stats,
)


def _registered_series(count: int) -> list[str]:
    return [series.series_id for series in load_definitions().series[:count]]


def _runs_store(path: Path, asof_dates: list[date]) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE screening_run(run_id TEXT, asof_date TEXT)")
        connection.executemany(
            "INSERT INTO screening_run(run_id, asof_date) VALUES (?, ?)",
            [(f"run-{index}", value.isoformat()) for index, value in enumerate(asof_dates)],
        )
    connection.close()


def _seed_unregistered_series(connection: sqlite3.Connection, series_id: str) -> None:
    """Leave a series row the registry no longer declares.

    ``provider_runs`` has a foreign key to ``series``, so a retired series can
    only carry run history while its store row survives the registry removal —
    which is exactly the state this filtering exists for.
    """

    connection.execute(
        "INSERT INTO series("
        "series_id, name, category, geography, frequency, unit, provider, "
        "provider_series_id, source_id, source_url"
        ") VALUES (?, ?, 'rates', 'jp', 'daily', 'percent', 'test', ?, 'test', "
        "'https://example.com')",
        (series_id, series_id, series_id),
    )


def _record(
    connection: sqlite3.Connection,
    series_id: str,
    status: str,
    finished_at: datetime,
) -> None:
    # ``record_provider_run`` stamps finished_at with "now", so the stored row is
    # rewritten to the instant this test needs.
    record_provider_run(
        connection,
        provider="test",
        series_id=series_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        started_at=finished_at,
        status=status,
        record_count=0 if status != "ok" else 1,
    )
    connection.execute(
        "UPDATE provider_runs SET finished_at = ? WHERE finished_at = ("
        "SELECT max(finished_at) FROM provider_runs)",
        (finished_at.isoformat(),),
    )


def test_store_stats_reports_row_count_and_newest_date(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    _runs_store(path, [date(2026, 7, 20), date(2026, 7, 21)])

    stats = store_stats("runs", path)

    assert stats.exists is True
    assert stats.row_count == 2
    assert stats.latest_date == date(2026, 7, 21)
    assert stats.size_bytes is not None
    assert stats.size_bytes > 0


def test_store_stats_reports_absent_store_without_raising(tmp_path: Path) -> None:
    stats = store_stats("runs", tmp_path / "missing.sqlite")

    assert stats.exists is False
    assert stats.row_count is None
    assert stats.latest_date is None
    assert stats.size_bytes is None


def test_store_stats_rejects_an_unknown_store_name(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    _runs_store(path, [date(2026, 7, 21)])

    with pytest.raises(ValueError, match="unknown store"):
        store_stats("screening_run; DROP TABLE screening_run", path)


def test_application_store_stats_reports_size_without_a_data_date(tmp_path: Path) -> None:
    path = tmp_path / "baibai.sqlite"
    sqlite3.connect(path).close()

    stats = application_store_stats(path)

    assert stats.exists is True
    assert stats.size_bytes is not None
    assert stats.row_count is None
    assert stats.latest_date is None


def test_provider_failure_streaks_counts_failures_back_to_the_last_success(
    tmp_path: Path,
) -> None:
    series_id = _registered_series(1)[0]
    path = tmp_path / "macro.sqlite"
    connection = initialize_database(path)
    base = datetime(2026, 7, 20, tzinfo=UTC)
    _record(connection, series_id, "ok", base)
    _record(connection, series_id, "failed", base + timedelta(days=1))
    _record(connection, series_id, "failed", base + timedelta(days=2))
    connection.commit()
    connection.close()

    streaks = provider_failure_streaks(path)

    assert [(item.series_id, item.consecutive_failures) for item in streaks] == [(series_id, 2)]
    assert streaks[0].failing_since == (base + timedelta(days=1)).isoformat()


def test_provider_failure_streaks_drops_a_series_whose_latest_run_succeeded(
    tmp_path: Path,
) -> None:
    series_id = _registered_series(1)[0]
    path = tmp_path / "macro.sqlite"
    connection = initialize_database(path)
    base = datetime(2026, 7, 20, tzinfo=UTC)
    _record(connection, series_id, "failed", base)
    _record(connection, series_id, "ok", base + timedelta(days=1))
    connection.commit()
    connection.close()

    assert provider_failure_streaks(path) == []


def test_provider_failure_streaks_ignores_a_series_missing_from_the_registry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "macro.sqlite"
    connection = initialize_database(path)
    _seed_unregistered_series(connection, "retired.series")
    _record(connection, "retired.series", "failed", datetime(2026, 7, 20, tzinfo=UTC))
    connection.commit()
    connection.close()

    assert provider_failure_streaks(path) == []


def test_provider_failure_streaks_orders_the_longest_outage_first(tmp_path: Path) -> None:
    first, second = _registered_series(2)
    path = tmp_path / "macro.sqlite"
    connection = initialize_database(path)
    base = datetime(2026, 7, 20, tzinfo=UTC)
    _record(connection, first, "failed", base)
    _record(connection, second, "failed", base + timedelta(days=1))
    _record(connection, second, "failed", base + timedelta(days=2))
    connection.commit()
    connection.close()

    streaks = provider_failure_streaks(path)

    assert [item.series_id for item in streaks] == [second, first]


def test_provider_failure_streaks_reports_nothing_without_a_store(tmp_path: Path) -> None:
    assert provider_failure_streaks(tmp_path / "missing.sqlite") == []


def test_store_stats_reports_unknown_depth_when_the_table_is_gone(tmp_path: Path) -> None:
    # An observability read must not fail the export that publishes the judgment
    # views, so a renamed or missing table degrades instead of raising.
    path = tmp_path / "runs.sqlite"
    sqlite3.connect(path).close()

    stats = store_stats("runs", path)

    assert stats.exists is True
    assert stats.row_count is None
    assert stats.latest_date is None
    assert stats.size_bytes is not None


def test_store_stats_reports_unknown_depth_for_a_non_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    path.write_bytes(b"not a database at all")

    stats = store_stats("runs", path)

    assert stats.exists is True
    assert stats.row_count is None


def test_store_stats_reports_unknown_depth_when_the_date_column_is_not_a_date(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.sqlite"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE screening_run(run_id TEXT, asof_date TEXT)")
        connection.execute("INSERT INTO screening_run VALUES ('run-1', 'not-a-date')")
    connection.close()

    stats = store_stats("runs", path)

    assert stats.row_count is None
    assert stats.latest_date is None


def test_provider_failure_streaks_reports_nothing_for_an_unreadable_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "macro.sqlite"
    path.write_bytes(b"not a database at all")

    assert provider_failure_streaks(path) == []


def test_never_attempted_series_lists_registered_series_without_any_run(
    tmp_path: Path,
) -> None:
    attempted, *_ = _registered_series(2)
    path = tmp_path / "macro.sqlite"
    connection = initialize_database(path)
    _record(connection, attempted, "ok", datetime(2026, 7, 20, tzinfo=UTC))
    connection.commit()
    connection.close()

    never = never_attempted_series(path)

    assert attempted not in never
    # Every other registered series has no run record at all.
    assert len(never) == len(load_definitions().series) - 1


def test_never_attempted_series_is_every_series_without_a_store(tmp_path: Path) -> None:
    assert never_attempted_series(tmp_path / "missing.sqlite") == sorted(
        series.series_id for series in load_definitions().series
    )


def test_every_store_table_exists_in_the_schema_that_owns_it(tmp_path: Path) -> None:
    """Bind the hard-coded table/column map to the real store schemas.

    ``store_stats`` degrades on a renamed table, so a rename would otherwise show
    up only as depth silently going blank in production. Building each store
    through its own schema keeps the map honest at merge time instead.
    """

    from baibai_engine.market.sqlite.schema import open_connection as open_market_store
    from baibai_engine.screening.run_store.store import initialize_run_store

    market_path = tmp_path / "market.sqlite"
    open_market_store(market_path).close()
    assert store_stats("market", market_path).row_count == 0

    runs_path = tmp_path / "runs.sqlite"
    initialize_run_store(runs_path)
    assert store_stats("runs", runs_path).row_count == 0

    macro_path = tmp_path / "macro.sqlite"
    initialize_database(macro_path).close()
    assert store_stats("macro", macro_path).row_count == 0
    # provider_failure_streaks reads a fourth table in the same store.
    assert provider_failure_streaks(macro_path) == []
