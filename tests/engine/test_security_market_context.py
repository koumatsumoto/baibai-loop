from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

from baibai_engine.machine_analysis.models.company_financials.metrics.history import (
    VALUATION_HISTORY_SESSIONS,
)
from baibai_engine.machine_analysis.models.market_context.security import build_security_market_context
from baibai_engine.machine_analysis.generation.runners.technical_analysis import technical_analysis
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import read_jpx_regulations, read_next_earnings_dates
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_ASOF = date(2026, 5, 29)


def _insert_bars(sqlite_path: Path, ticker: str, closes: list[float], *, end: date) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end, turnover_value=2.0e8)


def _insert_reference_rows(sqlite_path: Path) -> None:
    conn = open_connection(sqlite_path)
    try:
        insert_master_rows(
            conn, [("2026-05-28", "AAAA", "テスト製作所", "プライム", "機械", "011")]
        )
        insert_master_rows(conn, [("2026-05-28", "BBBB", "同業ペア", "プライム", "機械", "011")])
        conn.execute(
            "INSERT OR REPLACE INTO jpx_earnings_calendar"
            "(announcement_date, ticker) VALUES ('2026-06-10', 'AAAA')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO source_coverage("
            "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "jpx_earnings_calendar",
                "get_earnings_calendar_snapshot:current",
                "2026-06-10",
                "2026-06-10",
                "2026-05-29T00:00:00+09:00",
                1,
                "ok",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO jpx_regulation_flags"
            "(asof_date, source_name, ticker, flag)"
            " VALUES ('2026-05-29', '特別注意銘柄', 'AAAA', '特別注意銘柄')"
        )
        add_source_coverage(
            conn,
            source="jpx_regulation_flags",
            coverage_key="2026-05-29",
            min_date="2026-05-29",
            max_date="2026-05-29",
        )
        conn.commit()
    finally:
        conn.close()


class SecurityMarketContextTests(unittest.TestCase):
    def _build(self, root: Path, ticker: str) -> dict[str, object]:
        return build_security_market_context(
            sqlite_path=root / "market.sqlite",
            ticker=ticker,
            asof_date=_ASOF,
            self_range_window_bars=VALUATION_HISTORY_SESSIONS,
        )

    def test_legacy_jquants_earnings_rows_are_not_reported_as_jpx_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            with closing(open_connection(sqlite_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) "
                    "VALUES ('2026-06-10', 'AAAA')"
                )
                conn.execute(
                    "INSERT INTO source_coverage(source, coverage_key, coverage_start, "
                    "coverage_end, fetched_at_utc, record_count, status) "
                    "VALUES ('jpx_earnings_calendar', 'legacy', '2026-05-29', "
                    "'2026-08-27', '2026-05-29T00:00:00+09:00', 1, 'ok')"
                )
                conn.commit()

            self.assertIsNone(read_next_earnings_dates(sqlite_path, _ASOF))

    def test_security_and_technical_owners_preserve_price_and_relative_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            # 30 bars rising 1%/day; benchmark flat; one sector peer falling.
            _insert_bars(sqlite_path, "AAAA", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "1321", [200.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)

            profile = self._build(root, "AAAA")

            master = profile["master"]
            assert isinstance(master, dict)
            self.assertEqual(master["sector_33"], "機械")
            price = profile["price"]
            assert isinstance(price, dict)
            self.assertEqual(price["resolved_date"], _ASOF.isoformat())
            assert isinstance(price["return_5d"], float)
            self.assertAlmostEqual(price["return_5d"], 1.01**5 - 1, places=9)
            self.assertEqual(price["bar_count"], 30)
            self.assertAlmostEqual(price["avg_turnover_20d_oku"], 2.0, places=9)
            self.assertNotIn("bollinger_20bar", price)
            technical = technical_analysis(sqlite_path, ticker="AAAA", as_of=_ASOF)
            self.assertEqual(technical["rsi_14bar"]["value"], 100)
            self.assertEqual(technical["moving_averages"]["sma_20bar"]["status"], "available")
            self.assertEqual(
                technical["moving_averages"]["sma_200bar"]["status"], "insufficient_history"
            )
            relative = profile["relative"]
            assert isinstance(relative, dict)
            assert isinstance(relative["relative_5d"], float)
            self.assertAlmostEqual(relative["relative_5d"], 1.01**5 - 1, places=9)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertLess(sector["peer_median_return_20d"], 0)
            self.assertEqual(
                read_next_earnings_dates(sqlite_path, _ASOF)["AAAA"], date(2026, 6, 10)
            )
            self.assertEqual(
                read_jpx_regulations(sqlite_path, _ASOF).flags_by_ticker["AAAA"], ("特別注意銘柄",)
            )

    def test_empty_regulation_snapshot_replaces_older_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_reference_rows(sqlite_path)
            with closing(open_connection(sqlite_path)) as conn, conn:
                add_source_coverage(
                    conn,
                    source="jpx_regulation_flags",
                    coverage_key="2026-05-30",
                    min_date="2026-05-30",
                    max_date="2026-05-30",
                    record_count=0,
                )
            self.assertEqual(
                read_jpx_regulations(sqlite_path, date(2026, 5, 30)).flags_by_ticker, {}
            )
            self.assertEqual(
                read_jpx_regulations(sqlite_path, _ASOF).flags_by_ticker["AAAA"], ("特別注意銘柄",)
            )

    def test_price_series_keeps_an_action_event_without_a_close(self) -> None:
        """売買停止日のfactorを落とさず、前後価格を同じ株式基準へ揃える。"""

        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, turnover_value, adjustment_factor"
                    ") VALUES (?, ?, ?, ?, ?)",
                    [
                        ("6731", "2023-12-26", 1.0, 2.0e8, 1.0),
                        ("6731", "2023-12-27", None, None, 100.0),
                        ("6731", "2023-12-29", 100.0, 2.0e8, 1.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with_event = read_price_bars(
                sqlite_path,
                tickers=("6731",),
                start=date(2023, 12, 26),
                end=date(2023, 12, 29),
            )["6731"]
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET adjustment_factor = 1 "
                    "WHERE ticker = ? AND traded_at = ?",
                    ("6731", "2023-12-27"),
                )
                conn.commit()
            finally:
                conn.close()
            without_event = read_price_bars(
                sqlite_path,
                tickers=("6731",),
                start=date(2023, 12, 26),
                end=date(2023, 12, 29),
            )["6731"]

            self.assertEqual([bar.price for bar in with_event], [100.0, 100.0])
            self.assertEqual([bar.price for bar in without_event], [1.0, 100.0])

    def test_security_context_degrades_explicitly_for_unknown_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _insert_bars(root / "market.sqlite", "1321", [200.0] * 30, end=_ASOF)

            profile = self._build(root, "ZZZZ")

            self.assertIsNone(profile["master"])
            self.assertIsNone(profile["price"])
            self.assertIsNone(profile["relative"])

    def test_master_and_sector_peers_use_only_latest_global_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "AAAA", [100.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "CCCC", [200 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                insert_master_rows(
                    conn,
                    [
                        ("2026-05-29", "AAAA", "テスト製作所", "プライム", "機械", "011"),
                        ("2026-05-29", "CCCC", "現構成ペア", "プライム", "機械", "011"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            current_profile = self._build(root, "AAAA")
            old_only_profile = self._build(root, "BBBB")

            relative = current_profile["relative"]
            assert isinstance(relative, dict)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertGreater(sector["peer_median_return_20d"], 0)
            self.assertIsNone(old_only_profile["master"])


def _insert_dated_bars(sqlite_path, ticker, prices):
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES (?, ?, ?, 1)",
            [(ticker, day.isoformat(), price) for day, price in prices],
        )
        conn.commit()
    finally:
        conn.close()


def _security_context(root, asof):
    return build_security_market_context(
        sqlite_path=root / "market.sqlite",
        ticker="AAAA",
        asof_date=asof,
        self_range_window_bars=VALUATION_HISTORY_SESSIONS,
    )


@pytest.mark.parametrize("store", ["absent-file", "empty-file", "unwritten-header"])
def test_unwritten_market_store_has_no_price_or_earnings_observation(tmp_path, store):
    path = tmp_path / "market.sqlite"
    if store == "empty-file":
        sqlite3.connect(path).close()
    elif store == "unwritten-header":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
    before = path.read_bytes() if path.exists() else None
    profile = _security_context(tmp_path, _ASOF)
    assert read_next_earnings_dates(path, _ASOF) is None
    assert profile["price"] is None
    assert technical_analysis(path, ticker="AAAA", as_of=_ASOF)["bar_count"] == 0
    assert (path.read_bytes() if path.exists() else None) == before
    assert not (tmp_path / "runs.sqlite").exists()
    assert not (tmp_path / "app.sqlite").exists()


@pytest.mark.parametrize("state", ["missing", "stale"])
def test_unavailable_earnings_schedule_preserves_price_and_technical_facts(tmp_path, state):
    path = tmp_path / "market.sqlite"
    _insert_bars(path, "AAAA", [100.0] * 30, end=_ASOF)
    _insert_reference_rows(path)
    with closing(open_connection(path)) as connection, connection:
        if state == "missing":
            connection.execute("DELETE FROM source_coverage WHERE source = 'jpx_earnings_calendar'")
        else:
            connection.execute(
                "UPDATE source_coverage SET fetched_at_utc = '2026-05-01T00:00:00Z' "
                "WHERE source = 'jpx_earnings_calendar'"
            )
    profile = _security_context(tmp_path, _ASOF)
    assert read_next_earnings_dates(path, _ASOF) is None
    assert profile["price"]["bar_count"] == 30
    assert (
        technical_analysis(path, ticker="AAAA", as_of=_ASOF)["moving_averages"]["sma_20bar"][
            "status"
        ]
        == "available"
    )


@pytest.mark.parametrize("missing", [None, "start", "end"])
def test_relative_uses_exact_target_endpoints(tmp_path, missing):
    start, middle, end = (date(2026, 9, day) for day in (15, 16, 17))
    sqlite_path = tmp_path / "market.sqlite"
    _insert_dated_bars(sqlite_path, "AAAA", [(start, 100), (end, 121)])
    benchmark = [(start, 200), (middle, 220), (end, 242)]
    missing_date = {"start": start, "end": end}.get(missing)
    _insert_dated_bars(
        sqlite_path, "1321", [(day, price) for day, price in benchmark if day != missing_date]
    )
    profile = _security_context(tmp_path, end)
    assert profile["price"]["return_1d"] == pytest.approx(0.21)
    relative = profile["relative"]
    if missing is None:
        assert relative["relative_1d"] == pytest.approx(0)
    else:
        assert relative["relative_1d"] is None
    for window in (5, 20, 60):
        assert relative[f"relative_{window}d"] is None


@pytest.mark.parametrize("window", [1, 5, 20, 60])
def test_relative_loads_suspended_target_interval_and_ignores_later_bars(tmp_path, window):
    end = date(2026, 9, 17)
    start = end - timedelta(days=180)
    target = [(start, 100)] + [
        (end - timedelta(days=window - i - 1), 110 + i) for i in range(window)
    ]
    sqlite_path = tmp_path / "market.sqlite"
    _insert_dated_bars(sqlite_path, "AAAA", target)
    # Only endpoints are required, even when the benchmark has fewer bars than target.
    _insert_dated_bars(
        sqlite_path, "1321", [(start, 200), (end, 220), (end + timedelta(days=1), 500)]
    )
    profile = _security_context(tmp_path, end + timedelta(days=1))
    own_return = target[-1][1] / 100 - 1
    assert profile["price"][f"return_{window}d"] == pytest.approx(own_return)
    assert profile["relative"][f"relative_{window}d"] == pytest.approx(own_return - 0.1)


@pytest.mark.parametrize("target_count", [20, 21])
@pytest.mark.parametrize("usable_peers", [False, True])
def test_sector_uses_only_peers_with_exact_target_interval(tmp_path, target_count, usable_peers):
    sqlite_path = tmp_path / "market.sqlite"
    end = _ASOF
    start = end - timedelta(days=180)
    target = [(start, 100)] + [(end - timedelta(days=19 - i), 110) for i in range(20)]
    _insert_dated_bars(sqlite_path, "AAAA", target[-target_count:])
    _insert_reference_rows(sqlite_path)
    peers = {
        "BBBB": [(start, 100), (end, 120)],
        "CCCC": [(start, 100), (end, 140)],
        "DDDD": [(start + timedelta(days=1), 100), (end, 900)],
        "EEEE": [(start, 100), (end - timedelta(days=1), 900)],
    }
    if not usable_peers:
        peers.pop("BBBB")
        peers.pop("CCCC")
    for ticker, prices in peers.items():
        _insert_dated_bars(sqlite_path, ticker, prices)
    conn = open_connection(sqlite_path)
    try:
        insert_master_rows(
            conn, [("2026-05-28", ticker, "peer", "プライム", "機械", "011") for ticker in peers]
        )
        conn.commit()
    finally:
        conn.close()
    sector = _security_context(tmp_path, end)["relative"]["sector"]
    if target_count == 20 or not usable_peers:
        assert sector is None
    else:
        assert sector["peer_count"] == 2
        assert sector["peer_median_return_20d"] == pytest.approx(0.3)


if __name__ == "__main__":
    unittest.main()
