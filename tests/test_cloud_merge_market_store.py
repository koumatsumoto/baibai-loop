"""The market-store merge must lose no row from either side."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest
from tools.cloud.merge_market_store import (
    FACT_KEYS,
    UNCOMPARED,
    MergeError,
    main,
    merge_stores,
)

from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.screening.sqlite_cache import open_connection
from tests.helpers.screening_sqlite import add_source_coverage


def _store(path: Path) -> Path:
    open_connection(path).close()
    return path


def _add_bar(path: Path, ticker: str, traded_at: str, close: float) -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
            (ticker, traded_at, close, close),
        )
        conn.commit()
    finally:
        conn.close()


def _add_coverage(path: Path, source: str, coverage_key: str, *, record_count: int = 1) -> None:
    conn = open_connection(path)
    try:
        add_source_coverage(
            conn,
            source=source,
            coverage_key=coverage_key,
            record_count=record_count,
            fetched_at_utc="2026-07-31T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def _bars(path: Path) -> list[tuple[str, str, float]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            (str(row[0]), str(row[1]), float(row[2]))
            for row in conn.execute(
                "SELECT ticker, traded_at, close FROM jquants_daily_bars ORDER BY 1, 2"
            )
        ]
    finally:
        conn.close()


def test_every_market_table_is_merged(tmp_path: Path) -> None:
    """A table the store carries but the merge does not name would be dropped in silence."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    assert set(FACT_KEYS) == present


def test_each_declared_key_is_the_tables_primary_key(tmp_path: Path) -> None:
    """A key that is not the one SQLite enforces would let the merge duplicate rows."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, keys in FACT_KEYS.items():
            primary = tuple(
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]
            )
            assert set(keys) == set(primary), table
    finally:
        conn.close()


def test_a_row_only_the_source_has_is_carried_over(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)
    _add_bar(target, "7203", "2024-01-05", 200.0)

    report = merge_stores(source, target)

    assert _bars(target) == [("7203", "2020-01-06", 100.0), ("7203", "2024-01-05", 200.0)]
    assert report.inserted == 1


def test_a_row_only_the_target_has_is_kept(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(target, "7203", "2024-01-05", 200.0)

    merge_stores(source, target)

    assert _bars(target) == [("7203", "2024-01-05", 200.0)]


def test_a_shared_key_that_disagrees_stops_the_merge(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2024-01-05", 111.0)
    _add_bar(target, "7203", "2024-01-05", 222.0)
    _add_bar(source, "6758", "2020-01-06", 50.0)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)

    # The target keeps its own value and gains nothing: the merge is one transaction.
    assert _bars(target) == [("7203", "2024-01-05", 222.0)]


def test_coverage_rows_the_cloud_recorded_are_carried_over(tmp_path: Path) -> None:
    """Coverage decides what a later fetch may skip, so losing it re-fetches silently."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_coverage(source, "jquants_daily_bars", "2026-07-31")
    _add_coverage(target, "jquants_daily_bars", "2016-08-01")

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        keys = [str(row[0]) for row in conn.execute("SELECT coverage_key FROM source_coverage")]
    finally:
        conn.close()
    assert sorted(keys) == ["2016-08-01", "2026-07-31"]


def test_a_source_row_the_insert_could_not_place_stops_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check must fire when a row is ignored rather than inserted.

    Declaring a key wider than the one SQLite enforces reproduces that: the insert is
    ignored on the real primary key while the merge still considers the row unmatched.
    A store whose declared key drifts from its schema would otherwise publish quietly.
    """

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2024-01-05", 111.0)
    _add_bar(target, "7203", "2024-01-05", 222.0)
    wider: Mapping[str, tuple[str, ...]] = {"jquants_daily_bars": ("ticker", "traded_at", "close")}
    monkeypatch.setattr("tools.cloud.merge_market_store.FACT_KEYS", wider)

    with pytest.raises(MergeError, match="still missing after the merge"):
        merge_stores(source, target)

    assert _bars(target) == [("7203", "2024-01-05", 222.0)]


def test_a_store_on_another_schema_is_refused(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)
    conn = sqlite3.connect(source)
    try:
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MergeError, match="schema is"):
        merge_stores(source, target)

    assert _bars(target) == []


def test_a_store_whose_table_shape_drifted_is_refused(tmp_path: Path) -> None:
    """The merge inserts whole rows, so a store one column wider must not reach it."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    conn = sqlite3.connect(target)
    try:
        conn.execute("ALTER TABLE jquants_market_calendar ADD COLUMN extra TEXT")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MergeError, match="schema contract is invalid"):
        merge_stores(source, target)


def test_a_missing_store_is_named_rather_than_crashing(tmp_path: Path) -> None:
    target = _store(tmp_path / "target.sqlite")

    with pytest.raises(MergeError, match="does not exist"):
        merge_stores(tmp_path / "absent.sqlite", target)


def test_the_command_reports_a_refusal_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _store(tmp_path / "target.sqlite")

    code = main(["--source", str(tmp_path / "absent.sqlite"), "--target", str(target)])

    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_the_command_prints_what_it_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)

    code = main(["--source", str(source), "--target", str(target)])

    assert code == 0
    output = capsys.readouterr().out
    assert "jquants_daily_bars" in output
    assert "merged 1 rows" in output


def test_a_fact_column_that_disagrees_still_stops_the_merge(tmp_path: Path) -> None:
    """The exemptions must not reach a column the source actually asserts."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_coverage(source, "jquants_daily_bars", "2026-07-31", record_count=10)
    _add_coverage(target, "jquants_daily_bars", "2026-07-31", record_count=99)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)


def test_two_stores_that_read_the_same_day_at_different_times_still_merge(
    tmp_path: Path,
) -> None:
    """`fetched_at_utc` says when a store read, so it always differs and must not refuse."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    for path, fetched in (
        (source, "2026-07-30T14:43:18+00:00"),
        (target, "2026-07-31T13:30:36+00:00"),
    ):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="edinet_documents",
                coverage_key="2026-07-30",
                record_count=220,
                fetched_at_utc=fetched,
            )
            conn.commit()
        finally:
            conn.close()

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT fetched_at_utc FROM source_coverage").fetchone()
    finally:
        conn.close()
    assert kept is not None
    assert kept[0] == "2026-07-31T13:30:36+00:00"


def test_the_exempt_columns_are_only_the_ones_named(tmp_path: Path) -> None:
    """A column added to a table must be compared unless someone exempts it deliberately."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, exempt in UNCOMPARED.items():
            present = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert set(exempt) <= present, table
            assert set(exempt) & set(FACT_KEYS[table]) == set(), table
    finally:
        conn.close()
    assert set(UNCOMPARED) <= set(FACT_KEYS)
