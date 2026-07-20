from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.read_api.market import latest_unadjusted_closes


def _seed_bars(path: Path, rows: list[tuple[str, str, float | None]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE jquants_daily_bars("
            "ticker TEXT NOT NULL, traded_at TEXT NOT NULL, close REAL, "
            "PRIMARY KEY (ticker, traded_at))"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_latest_unadjusted_closes_returns_newest_non_null_close_per_ticker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars(
        database,
        [
            ("2331", "2026-07-14", 1000.0),
            ("2331", "2026-07-17", 1055.0),
            ("2331", "2026-07-18", None),  # NULL close never wins over an earlier real close
            ("4432", "2026-07-16", 2500.5),
            ("9999", "2026-07-10", 300.0),  # not requested, stays out of the result
        ],
    )

    result = latest_unadjusted_closes(database, ["2331", "4432", "0000"])

    assert result == {
        "2331": (1055.0, date(2026, 7, 17)),
        "4432": (2500.5, date(2026, 7, 16)),
    }


def test_latest_unadjusted_closes_is_empty_without_file_or_tickers(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    assert latest_unadjusted_closes(missing, ["2331"]) == {}
    assert latest_unadjusted_closes(missing, []) == {}
