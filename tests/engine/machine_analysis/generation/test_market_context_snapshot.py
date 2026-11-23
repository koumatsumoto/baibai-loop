from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.cli.market_snapshot import (
    build_parser,
    market_snapshot_command,
)
from baibai_engine.machine_analysis.generation.runners import market_snapshot as snapshot_runner
from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    build_market_snapshot,
)
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_ASOF = date(2026, 5, 29)


def _insert_bars(sqlite_path: Path, ticker: str, closes: list[float], *, end: date) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end)


def _insert_sector(sqlite_path: Path, ticker: str, sector: str) -> None:
    conn = open_connection(sqlite_path)
    try:
        insert_master_rows(
            conn, [("2026-05-28", ticker, f"name-{ticker}", "プライム", sector, "011")]
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("before_lookup", ["current", "historical"])
def test_raw_snapshot_cannot_mix_old_prices_and_new_master_membership(
    tmp_path, monkeypatch, before_lookup
):
    path = tmp_path / "market.sqlite"
    prior = _ASOF - timedelta(days=7)
    _insert_bars(path, "1321", [100.0] * 31, end=_ASOF)
    _insert_bars(path, "1234", [100.0] * 30 + [150.0], end=_ASOF)
    original = snapshot_runner.read_master_snapshot
    race_day = _ASOF if before_lookup == "current" else prior

    def view():
        return build_market_snapshot(
            sqlite_path=path, asof_date=_ASOF, history_weeks=2, min_breadth_sample=1
        )

    with closing(open_connection(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        with writer:
            insert_master_rows(
                writer,
                [
                    (day.isoformat(), "1234", "test", "Prime", "機械", "011")
                    for day in (prior, _ASOF)
                ],
            )
        before = view()

        def read_during_replacement(*args, as_of, **kwargs):
            if as_of == race_day:
                with writer:
                    writer.execute(
                        "UPDATE jquants_daily_bars SET close=50 WHERE ticker='1234' AND traded_at=?",
                        (_ASOF.isoformat(),),
                    )
                    writer.execute(
                        "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?",
                        (prior.isoformat(),),
                    )
                    insert_master_rows(
                        writer,
                        [
                            (_ASOF.isoformat(), "1234", "test", "Prime", "建設業", "011"),
                            (prior.isoformat(), "5678", "new", "Prime", "機械", "011"),
                        ],
                    )
            return original(*args, as_of=as_of, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(snapshot_runner, "read_master_snapshot", read_during_replacement)
            observed = view()
        after = view()
    assert before["points"][0]["breadth_sample_size"] == 1
    assert after["points"][0]["breadth_sample_size"] == 0
    assert before["sectors"][0]["median_return_20d"] == 0.5
    assert after["sectors"][0]["median_return_20d"] == -0.5
    assert before["sectors"][0]["sector_33"] == "機械"
    assert after["sectors"][0]["sector_33"] == "建設業"
    assert observed == before


def test_inferred_asof_and_snapshot_cannot_straddle_a_price_withdrawal(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    _insert_bars(path, "1321", [100.0] * 22 + [150.0], end=_ASOF)
    # MAX() has positioned the first row. Withdraw its trading day before the
    # command reads the bars and terminal breadth/return inputs.
    connect = sqlite3.connect
    interleaved = False

    def read():
        stdout = io.StringIO()
        assert (
            market_snapshot_command(
                asof=None, weeks=1, sqlite_path=path, stdout=stdout, output_format="json"
            )
            == 0
        )
        return json.loads(stdout.getvalue())

    with closing(open_connection(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        before = read()

        class InterleavedRead(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                nonlocal interleaved
                cursor = super().execute(sql, parameters)
                if sql.startswith("SELECT MAX(traded_at) FROM jquants_daily_bars"):
                    assert not interleaved
                    interleaved = True
                    with writer:
                        writer.execute(
                            "DELETE FROM jquants_daily_bars WHERE traded_at=?", (_ASOF.isoformat(),)
                        )
                return cursor

        def interleave(*args, **kwargs):
            return connect(*args, **kwargs, factory=InterleavedRead)

        with monkeypatch.context() as patcher:
            patcher.setattr(sqlite3, "connect", interleave)
            observed = read()
        after = read()
    assert interleaved
    assert before["as_of"] == _ASOF.isoformat()
    assert before["points"][-1]["observation_date"] == _ASOF.isoformat()
    assert after["as_of"] == (_ASOF - timedelta(days=1)).isoformat()
    assert observed == before


class BuildMarketSnapshotTests(unittest.TestCase):
    def test_close_null_adjustment_event_is_applied_to_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [1.0] * 50 + [100.0] * 10, end=_ASOF)
            conn = open_connection(sqlite_path)
            try:
                event_day = date(2026, 5, 20)
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_daily_bars"
                    "(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, NULL, 100.0)",
                    ("1321", event_day.isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=1,
            )

            points = payload["points"]
            assert isinstance(points, list)
            self.assertAlmostEqual(float(points[-1]["benchmark_return_20d"]), 0.0, places=12)

    def test_points_and_sectors_reflect_trend_and_breadth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # benchmark up ~0.3%/day (>3% per 20 bars -> rally), one rising and
            # one falling sector member.
            _insert_bars(sqlite_path, "1321", [100 * 1.003**i for i in range(120)], end=_ASOF)
            _insert_bars(sqlite_path, "AAAA", [50 * 1.005**i for i in range(120)], end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [80 * 0.997**i for i in range(120)], end=_ASOF)
            _insert_sector(sqlite_path, "AAAA", "機械")
            _insert_sector(sqlite_path, "BBBB", "小売業")

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=4,
                min_breadth_sample=2,
            )

            points = payload["points"]
            assert isinstance(points, list)
            self.assertEqual(len(points), 4)
            self.assertEqual(points[-1]["date"], _ASOF.isoformat())
            self.assertLess(points[0]["date"], points[-1]["date"])
            last = points[-1]
            assert isinstance(last["benchmark_return_20d"], float)
            self.assertGreater(last["benchmark_return_20d"], 0.03)
            self.assertEqual(last["benchmark_trend"], "uptrend")
            self.assertEqual(last["breadth_sample_size"], 2)
            assert isinstance(last["breadth_pct_above_ma20"], float)

            sectors = payload["sectors"]
            assert isinstance(sectors, list)
            self.assertEqual([row["sector_33"] for row in sectors], ["機械", "小売業"])
            rising = sectors[0]
            assert isinstance(rising["median_return_20d"], float)
            self.assertGreater(rising["median_return_20d"], 0)
            self.assertEqual(rising["pct_above_ma20"], 1.0)
            falling = sectors[1]
            assert isinstance(falling["median_return_20d"], float)
            self.assertLess(falling["median_return_20d"], 0)

    def test_breadth_degrades_below_min_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=5,
            )
            points = payload["points"]
            assert isinstance(points, list)
            self.assertIsNone(points[0]["breadth_pct_above_ma20"])
            self.assertEqual(points[0]["benchmark_trend"], "neutral")

    def test_sectors_do_not_restore_tickers_from_older_master_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "OLD1", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "LIVE", [100 * 1.01**i for i in range(30)], end=_ASOF)
            conn = open_connection(sqlite_path)
            try:
                insert_master_rows(
                    conn,
                    [
                        ("2026-05-28", "OLD1", "旧構成銘柄", "プライム", "旧構成セクター", "011"),
                        ("2026-05-29", "LIVE", "現構成銘柄", "プライム", "機械", "011"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=1,
            )

            sectors = payload["sectors"]
            assert isinstance(sectors, list)
            self.assertEqual([row["sector_33"] for row in sectors], ["機械"])


class MarketSnapshotCliTests(unittest.TestCase):
    def test_parser_accepts_market_snapshot_arguments(self) -> None:
        args = build_parser().parse_args(["--asof", "2026-05-29", "--weeks", "8"])
        self.assertEqual(args.weeks, 8)

    def test_command_emits_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)
            buffer = io.StringIO()
            exit_code = market_snapshot_command(
                asof=_ASOF.isoformat(),
                weeks=2,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(payload["as_of"], _ASOF.isoformat())
            self.assertEqual(len(payload["points"]), 2)

    def test_command_emits_one_json_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)
            _insert_bars(sqlite_path, "1234", [100.0] * 61, end=_ASOF)
            _insert_sector(sqlite_path, "1234", "機械")
            buffer = io.StringIO()
            exit_code = market_snapshot_command(
                asof=_ASOF.isoformat(),
                weeks=2,
                sqlite_path=sqlite_path,
                stdout=buffer,
                output_format="json",
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["as_of"], _ASOF.isoformat())
            self.assertEqual(payload["sectors"][0]["observation_date"], _ASOF.isoformat())

    def test_default_asof_keeps_actual_old_date_and_excludes_future_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)
            _insert_bars(sqlite_path, "FUTURE", [200.0], end=date(2027, 1, 4))
            buffer = io.StringIO()
            with patch("baibai_engine.cli.market_snapshot.datetime") as clock:
                clock.now.return_value = datetime(2026, 10, 27, tzinfo=JST)
                exit_code = market_snapshot_command(
                    asof=None, weeks=1, sqlite_path=sqlite_path, stdout=buffer
                )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(payload["as_of"], _ASOF.isoformat())
            self.assertEqual(payload["points"][-1]["date"], _ASOF.isoformat())

    def test_default_asof_rejects_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(
                market_snapshot_command(
                    asof=None,
                    weeks=1,
                    sqlite_path=Path(tmpdir) / "missing.sqlite",
                    stdout=io.StringIO(),
                ),
                1,
            )

    def test_command_rejects_non_positive_weeks(self) -> None:
        exit_code = market_snapshot_command(
            asof=_ASOF.isoformat(),
            weeks=0,
            sqlite_path=Path("/nonexistent.sqlite"),
            stdout=io.StringIO(),
        )
        self.assertEqual(exit_code, 1)


class SectorObservationDateTests(unittest.TestCase):
    def test_only_current_members_enter_returns_and_breadth_with_or_without_benchmark(self):
        for benchmark in (True, False):
            for asof in (date(2026, 9, 18), date(2026, 9, 20)):
                with (
                    self.subTest(benchmark=benchmark, asof=asof),
                    tempfile.TemporaryDirectory() as tmpdir,
                ):
                    path = Path(tmpdir) / "market.sqlite"
                    _insert_bars(path, "LIVE", [100.0] * 70, end=date(2026, 9, 18))
                    _insert_bars(path, "OLD1", [100.0] * 69 + [300.0], end=date(2026, 9, 10))
                    for ticker in ("LIVE", "OLD1"):
                        _insert_sector(path, ticker, "建設業")
                    if benchmark:
                        _insert_bars(path, "1321", [100.0] * 70, end=date(2026, 9, 18))
                    payload = build_market_snapshot(
                        sqlite_path=path, asof_date=asof, history_weeks=1
                    )
                    self.assertEqual(
                        payload["sectors"],
                        [
                            {
                                "sector_33": "建設業",
                                "observation_date": "2026-09-18",
                                "ticker_count": 1,
                                "median_return_20d": 0.0,
                                "median_return_60d": 0.0,
                                "pct_above_ma20": 0.0,
                                "sample_sizes": {
                                    "median_return_20d": 1,
                                    "median_return_60d": 1,
                                    "pct_above_ma20": 1,
                                },
                            }
                        ],
                    )

    def test_benchmark_observation_date_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market.sqlite"
            _insert_bars(path, "1321", [100.0] * 70, end=date(2026, 9, 17))
            _insert_bars(path, "LIVE", [100.0] * 70, end=date(2026, 9, 17))
            _insert_bars(path, "LATE", [100.0] * 69 + [300.0], end=date(2026, 9, 18))
            for ticker in ("LIVE", "LATE"):
                _insert_sector(path, ticker, "建設業")
            payload = build_market_snapshot(
                sqlite_path=path, asof_date=date(2026, 9, 18), history_weeks=1
            )
            self.assertEqual(payload["sectors"][0]["observation_date"], "2026-09-17")
            self.assertEqual(payload["sectors"][0]["ticker_count"], 1)
            self.assertEqual(payload["sectors"][0]["median_return_20d"], 0.0)


if __name__ == "__main__":
    unittest.main()


def test_breadth_uses_each_points_common_stock_membership(tmp_path):
    path = tmp_path / "market.sqlite"
    for ticker, prices in (
        ("1321", [100 * 1.003**i for i in range(70)]),
        ("FUND", [100 * 1.01**i for i in range(70)]),
        ("OLD1", [100 * 1.01**i for i in range(70)]),
        ("LIVE", [100.0] * 70),
    ):
        _insert_bars(path, ticker, prices, end=_ASOF)
    conn = open_connection(path)
    try:
        insert_master_rows(
            conn,
            [
                ("2026-05-15", "OLD1", "old member", "PRIME", "機械", "011"),
                ("2026-05-15", "FUND", "fund", "PRIME", "その他", "014"),
                ("2026-05-29", "LIVE", "current member", "PRIME", "小売業", "011"),
                ("2026-05-29", "FUND", "fund", "PRIME", "その他", "014"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    result = build_market_snapshot(
        sqlite_path=path, asof_date=_ASOF, history_weeks=2, min_breadth_sample=1
    )
    assert [point["breadth_sample_size"] for point in result["points"]] == [1, 1]
    assert [point["breadth_pct_above_ma20"] for point in result["points"]] == [1.0, 0.0]
    assert [sector["sector_33"] for sector in result["sectors"]] == ["小売業"]


def test_sector_sample_sizes_follow_each_metric_not_the_short_return_population(tmp_path):
    path = tmp_path / "market.sqlite"
    # A 20-close series contributes to MA20 breadth but cannot supply 20 returns.
    for ticker, count in (("LONG", 61), ("SHRT", 21), ("MA20", 20)):
        _insert_bars(path, ticker, [100.0] * (count - 1) + [110.0], end=_ASOF)
        _insert_sector(path, ticker, "機械")
    _insert_bars(path, "NEW1", [100.0] * 21, end=_ASOF)
    _insert_sector(path, "NEW1", "小売業")
    _insert_bars(path, "OLD1", [100.0] * 61, end=date(2026, 5, 28))
    _insert_sector(path, "OLD1", "機械")

    sectors = build_market_snapshot(sqlite_path=path, asof_date=_ASOF)["sectors"]
    machinery, retail = sectors
    assert machinery["sector_33"] == "機械"
    assert machinery["ticker_count"] == 2
    assert machinery["sample_sizes"] == {
        "median_return_20d": 2,
        "median_return_60d": 1,
        "pct_above_ma20": 3,
    }
    assert machinery["median_return_20d"] == machinery["median_return_60d"] == 110 / 100 - 1
    assert machinery["pct_above_ma20"] == 1.0
    assert retail["sample_sizes"] == {
        "median_return_20d": 1,
        "median_return_60d": 0,
        "pct_above_ma20": 1,
    }
    assert retail["median_return_60d"] is None


def test_breadth_can_rise_with_unchanged_close_when_its_average_falls(tmp_path):
    path = tmp_path / "market.sqlite"
    days = sorted(
        day for offset in range(40) if (day := _ASOF - timedelta(days=offset)).weekday() < 5
    )[-25:]
    closes = [110.0] * 5 + [100.0] * 15 + [99.0] * 4 + [100.0]
    assert days[-6] == _ASOF - timedelta(days=7)
    assert closes[-6] == closes[-1] == 100.0
    with closing(open_connection(path)) as connection, connection:
        insert_master_rows(
            connection, [("2026-05-01", "AAAA", "same member", "PRIME", "機械", "011")]
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES (?,?,?)",
            [
                (ticker, day.isoformat(), close)
                for ticker, values in (("AAAA", closes), ("1321", [100.0] * len(days)))
                for day, close in zip(days, values, strict=True)
            ],
        )
    result = build_market_snapshot(
        sqlite_path=path, asof_date=_ASOF, history_weeks=2, min_breadth_sample=1
    )
    points = result["points"]
    assert [point["breadth_sample_size"] for point in points] == [1, 1]
    assert [point["breadth_pct_above_ma20"] for point in points] == [0.0, 1.0]
    # Membership and endpoint price are unchanged; this is not a 100% price gain.
    assert sum(closes[:20]) / 20 > closes[-6]
    assert sum(closes[-20:]) / 20 < closes[-1]


def test_sector_can_keep_independent_metrics_without_one_short_return(tmp_path):
    path = tmp_path / "market.sqlite"
    _insert_bars(path, "1321", [100.0] * 61, end=_ASOF)
    inputs = (
        ("SHRT", "小売業", [100.0] * 20 + [80.0]),
        ("MA20", "海運業", [100.0] * 19 + [110.0]),
        # Invalid at the short endpoint and within MA20, but not at either long endpoint.
        ("LONG", "機械", [80.0] + [100.0] * 39 + [0.0] + [100.0] * 17 + [0.0, 100.0, 100.0]),
        ("NONE", "医薬品", [100.0] * 19),
    )
    for ticker, sector, prices in inputs:
        _insert_bars(path, ticker, prices, end=_ASOF)
        _insert_sector(path, ticker, sector)

    shared = build_market_snapshot(sqlite_path=path, asof_date=_ASOF, history_weeks=1)
    observed = {row["sector_33"]: row for row in shared["sectors"]}
    assert list(observed) == ["小売業", "機械", "海運業"]
    assert observed["小売業"]["median_return_20d"] == 80.0 / 100.0 - 1
    for sector in ("機械", "海運業"):
        row = observed[sector]
        assert row["observation_date"] == _ASOF.isoformat()
        assert row["ticker_count"] == 0
        assert row["median_return_20d"] is None
        assert row["sample_sizes"]["median_return_20d"] == 0
    assert observed["機械"]["median_return_60d"] == 0.25
    assert observed["機械"]["sample_sizes"]["median_return_60d"] == 1
    assert observed["機械"]["pct_above_ma20"] is None
    assert observed["機械"]["sample_sizes"]["pct_above_ma20"] == 0
    assert observed["海運業"]["median_return_60d"] is None
    assert observed["海運業"]["sample_sizes"]["median_return_60d"] == 0
    assert observed["海運業"]["pct_above_ma20"] == 1.0
    assert observed["海運業"]["sample_sizes"]["pct_above_ma20"] == 1
