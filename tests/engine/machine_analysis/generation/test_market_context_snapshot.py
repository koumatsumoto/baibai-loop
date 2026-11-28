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
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes, seed_daily_bars

from baibai_engine.cli.app import main as engine_cli
from baibai_engine.cli.market_snapshot import (
    build_parser,
    market_snapshot_command,
)
from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    CurrentMasterReader,
    build_market_snapshot,
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.stocks.current_reader import read_current_jp_master
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    encode_current_listing_master,
)
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_ASOF = date(2026, 5, 29)


def _insert_bars(sqlite_path: Path, ticker: str, closes: list[float], *, end: date) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end)


def _current_section(
    sectors: dict[str, str],
    *,
    session: date = _ASOF,
    received_at: datetime | None = None,
    funds: tuple[str, ...] = (),
) -> CurrentListingMarket:
    listing = ListingSlice(
        "JP",
        session,
        tuple(
            ListingMasterRow(
                ("XTKS", code),
                f"合成企業-{code}",
                "etf" if code in funds else "common_stock",
                "listed",
                japan=JapanListingAttributes("014" if code in funds else "011", "プライム", sector),
            )
            for code, sector in sectors.items()
        ),
    )
    return CurrentListingMarket(
        listing,
        "jquants",
        received_at or datetime.combine(session, datetime.min.time(), tzinfo=JST),
        len(listing.rows),
    )


def _current_listing(
    path: Path,
    *,
    session: date = _ASOF,
    received_at: datetime | None = None,
    code: str = "12340",
) -> None:
    section = _current_section({code: "建設業"}, session=session, received_at=received_at)
    path.write_bytes(encode_current_listing_master({"JP": section}))


def _current_reader(section: CurrentListingMarket | None) -> CurrentMasterReader:
    def read(connection: sqlite3.Connection, asof: date):
        return read_current_jp_master(
            section,
            calendar=connection,
            as_of=asof,
            evaluation_time=datetime(asof.year, asof.month, asof.day, 18, tzinfo=JST),
        )

    return read


def _calendar(sqlite_path: Path, days: dict[date, str]) -> None:
    with closing(open_connection(sqlite_path)) as connection, connection:
        connection.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar VALUES (?, ?)",
            [(day.isoformat(), division) for day, division in days.items()],
        )


@pytest.mark.parametrize("infer_price_day", [False, True])
def test_raw_price_and_current_master_calendar_use_one_snapshot(tmp_path, infer_price_day):
    path = tmp_path / "market.sqlite"
    _insert_bars(path, "1321", [100.0] * 30 + [150.0], end=_ASOF)
    _insert_bars(path, "1234", [100.0] * 30 + [150.0], end=_ASOF)
    _calendar(path, {_ASOF - timedelta(days=1): "1", _ASOF: "1"})
    read_current = _current_reader(_current_section({"12340": "機械"}))

    def view(reader=read_current):
        return build_market_snapshot(
            sqlite_path=path,
            asof_date=_ASOF,
            history_weeks=2,
            min_breadth_sample=1,
            use_latest_cached_day=infer_price_day,
            read_current_master=reader,
        )

    with closing(open_connection(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        before = view()

        def read_during_correction(connection, asof):
            assert asof == _ASOF
            # After the cursor has read prices, replace both the bar and the session
            # classification. The reader must still see the single pre-change view.
            with writer:
                writer.execute(
                    "UPDATE jquants_daily_bars SET close=75 WHERE ticker='1321' AND traded_at=?",
                    (_ASOF.isoformat(),),
                )
                writer.execute(
                    "UPDATE jquants_market_calendar SET holiday_division='0' WHERE day=?",
                    (_ASOF.isoformat(),),
                )
            return read_current(connection, asof)

        observed = view(read_during_correction)
        after = view()

    assert before["master_snapshot_date"] == str(_ASOF)
    assert [point["breadth_sample_size"] for point in before["points"]] == [0, 1]
    assert before["points"][-1]["benchmark_return_20d"] == 0.5
    assert before["sectors"][0]["median_return_20d"] == 0.5
    assert observed == before
    assert after["points"][-1]["benchmark_return_20d"] == -0.25
    assert after["points"][-1]["breadth_sample_size"] == 0
    assert after["master_snapshot_date"] is None
    assert after["sectors"] == []


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
            _calendar(sqlite_path, {_ASOF: "1"})

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=4,
                min_breadth_sample=2,
                read_current_master=_current_reader(
                    _current_section({"AAAA0": "機械", "BBBB0": "小売業"})
                ),
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
            _insert_bars(sqlite_path, "1234", [100.0] * 30, end=_ASOF)
            _calendar(sqlite_path, {_ASOF: "1"})

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=5,
                read_current_master=_current_reader(_current_section({"12340": "機械"})),
            )
            points = payload["points"]
            assert isinstance(points, list)
            self.assertEqual(points[0]["breadth_sample_size"], 1)
            self.assertIsNone(points[0]["breadth_pct_above_ma20"])
            self.assertEqual(points[0]["benchmark_trend"], "neutral")

    def test_sectors_ignore_live_prices_of_symbols_outside_the_current_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "OLD1", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "LIVE", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _calendar(sqlite_path, {_ASOF: "1"})

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=1,
                read_current_master=_current_reader(_current_section({"LIVE0": "機械"})),
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
            _calendar(sqlite_path, {_ASOF: "1"})
            listing = Path(tmpdir) / "current.json"
            _current_listing(listing)
            buffer = io.StringIO()
            exit_code = market_snapshot_command(
                asof=_ASOF.isoformat(),
                weeks=2,
                sqlite_path=sqlite_path,
                stdout=buffer,
                output_format="json",
                current_listing_master=listing,
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
                    friday = date(2026, 9, 18)
                    _calendar(path, {friday: "1", date(2026, 9, 19): "0", date(2026, 9, 20): "0"})
                    if benchmark:
                        _insert_bars(path, "1321", [100.0] * 70, end=friday)
                    payload = build_market_snapshot(
                        sqlite_path=path,
                        asof_date=asof,
                        history_weeks=1,
                        read_current_master=_current_reader(
                            _current_section({"LIVE0": "建設業", "OLD10": "建設業"}, session=friday)
                        ),
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
            friday = date(2026, 9, 18)
            thursday = friday - timedelta(days=1)
            _insert_bars(path, "1321", [100.0] * 70, end=thursday)
            _insert_bars(path, "LIVE", [100.0] * 70, end=thursday)
            _insert_bars(path, "LATE", [100.0] * 69 + [300.0], end=friday)
            _calendar(path, {friday: "1"})
            reader = _current_reader(
                _current_section({"LIVE0": "建設業", "LATE0": "建設業"}, session=friday)
            )

            def view():
                return build_market_snapshot(
                    sqlite_path=path, asof_date=friday, history_weeks=1, read_current_master=reader
                )

            lagging = view()
            self.assertEqual(lagging["points"][0]["observation_date"], str(thursday))
            self.assertEqual(lagging["sectors"], [])
            seed_daily_bars(path, [("1321", str(friday), 100, 1)])
            (sector,) = view()["sectors"]
            self.assertEqual(sector["observation_date"], str(friday))
            self.assertEqual(sector["ticker_count"], 1)
            self.assertEqual(sector["median_return_20d"], 2.0)


if __name__ == "__main__":
    unittest.main()


def test_weekly_breadth_does_not_project_current_domestic_common_stock_backward(tmp_path):
    path = tmp_path / "market.sqlite"
    for ticker, prices in (
        ("1321", [100 * 1.003**i for i in range(70)]),
        ("FUND", [100 * 1.01**i for i in range(70)]),
        ("OLD1", [100 * 1.01**i for i in range(70)]),
        ("LIVE", [100.0] * 70),
    ):
        _insert_bars(path, ticker, prices, end=_ASOF)
    _calendar(path, {_ASOF: "1"})
    result = build_market_snapshot(
        sqlite_path=path,
        asof_date=_ASOF,
        history_weeks=2,
        min_breadth_sample=1,
        read_current_master=_current_reader(
            _current_section({"LIVE0": "小売業", "FUND0": "その他"}, funds=("FUND0",))
        ),
    )
    assert [point["breadth_sample_size"] for point in result["points"]] == [0, 1]
    assert [point["breadth_pct_above_ma20"] for point in result["points"]] == [None, 0.0]
    assert result["points"][0]["benchmark_return_20d"] is not None
    assert [sector["sector_33"] for sector in result["sectors"]] == ["小売業"]


@pytest.mark.parametrize("prepared_read", [False, True])
@pytest.mark.parametrize("has_friday_master", [False, True])
def test_breadth_never_uses_a_master_snapshot_later_than_its_benchmark_close(
    tmp_path, prepared_read, has_friday_master
):
    path = tmp_path / "market.sqlite"
    friday = _ASOF
    monday = friday + timedelta(days=3)
    for ticker, closes in (
        ("1321", [100.0] * 65),
        ("1234", [100.0] * 64 + [125.0]),
        ("5678", [100.0] * 65),
    ):
        _insert_bars(path, ticker, closes, end=friday)
    _calendar(path, {friday: "1", monday: "1"})
    friday_section = _current_section(
        {"12340": "機械", "56780": "小売業"}, session=friday, funds=("56780",)
    )
    monday_section = _current_section(
        {"12340": "機械", "56780": "小売業"}, session=monday, funds=("12340",)
    )
    # A cached price received for beyond the requested date must not alter the basis.
    seed_daily_bars(path, [("1321", str(monday + timedelta(days=1)), 200, 1)])

    def snapshot(asof, section):
        read_current = _current_reader(section)
        prepared = None
        if prepared_read:
            with closing(open_connection(path)) as connection:
                master = read_current(connection, asof)
            prepared = prepare_market_context_from_bars(
                sqlite_path=path,
                asof_date=asof,
                master=master,
                bars_by_ticker=read_price_bars(path, start=date.min, end=asof),
                subjects=("1234", "5678"),
                history_weeks=1,
            )
        return build_market_snapshot(
            sqlite_path=path,
            asof_date=asof,
            history_weeks=1,
            min_breadth_sample=1,
            prepared=prepared,
            read_current_master=None if prepared_read else read_current,
        )

    before = snapshot(friday, friday_section if has_friday_master else monday_section)
    delayed = snapshot(monday, monday_section)
    old = before["points"][0]
    assert old["breadth_pct_above_ma20"] == (1.0 if has_friday_master else None)
    assert old["breadth_sample_size"] == int(has_friday_master)
    assert delayed["points"][0] == {
        **old,
        "date": str(monday),
        "breadth_pct_above_ma20": None,
        "breadth_sample_size": 0,
    }
    assert delayed["sectors"] == []
    assert delayed["master_snapshot_date"] == str(monday)

    # The fresh cohort really does change after the matching closes arrive. The
    # retired ordinary share and Tuesday's receipt cannot change that comparison.
    seed_daily_bars(
        path,
        [
            (ticker, str(monday), close, 1)
            for ticker, close in (("1321", 110), ("1234", 175), ("5678", 100))
        ],
    )
    refreshed = snapshot(monday, monday_section)
    assert [row["sector_33"] for row in refreshed["sectors"]] == ["小売業"]
    fresh = refreshed["points"][0]
    assert fresh["observation_date"] == str(monday)
    assert fresh["breadth_sample_size"] == 1
    assert fresh["breadth_pct_above_ma20"] == 0.0
    assert fresh["benchmark_return_20d"] == pytest.approx(0.1)


def test_sector_sample_sizes_follow_each_metric_not_the_short_return_population(tmp_path):
    path = tmp_path / "market.sqlite"
    # A 20-close series contributes to MA20 breadth but cannot supply 20 returns.
    for ticker, count in (("LONG", 61), ("SHRT", 21), ("MA20", 20)):
        _insert_bars(path, ticker, [100.0] * (count - 1) + [110.0], end=_ASOF)
    _insert_bars(path, "NEW1", [100.0] * 21, end=_ASOF)
    _insert_bars(path, "OLD1", [100.0] * 61, end=date(2026, 5, 28))
    _calendar(path, {_ASOF: "1"})

    sectors = build_market_snapshot(
        sqlite_path=path,
        asof_date=_ASOF,
        read_current_master=_current_reader(
            _current_section(
                {
                    "LONG0": "機械",
                    "SHRT0": "機械",
                    "MA200": "機械",
                    "OLD10": "機械",
                    "NEW10": "小売業",
                }
            )
        ),
    )["sectors"]
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
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES (?,?,?)",
            [
                (ticker, day.isoformat(), close)
                for ticker, values in (("AAAA", closes), ("1321", [100.0] * len(days)))
                for day, close in zip(days, values, strict=True)
            ],
        )
    _calendar(path, {days[-6]: "1", _ASOF: "1"})
    # Two independent, exact-session evaluations, not one run replaying old memberships.
    points = [
        build_market_snapshot(
            sqlite_path=path,
            asof_date=session,
            history_weeks=1,
            min_breadth_sample=1,
            read_current_master=_current_reader(
                _current_section({"AAAA0": "機械"}, session=session)
            ),
        )["points"][0]
        for session in (days[-6], _ASOF)
    ]
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
    for ticker, _, prices in inputs:
        _insert_bars(path, ticker, prices, end=_ASOF)
    _calendar(path, {_ASOF: "1"})

    shared = build_market_snapshot(
        sqlite_path=path,
        asof_date=_ASOF,
        history_weeks=1,
        read_current_master=_current_reader(
            _current_section({f"{ticker}0": sector for ticker, sector, _ in inputs})
        ),
    )
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


@pytest.mark.parametrize(
    "basis",
    ["omitted", "missing", "malformed", "invalid_row", "future_receipt", "wrong_session"],
)
def test_public_current_snapshot_keeps_prices_without_unconfirmed_taxonomy(tmp_path, capsys, basis):
    market = tmp_path / "market.sqlite"
    listing = tmp_path / "private-artifact.json"
    _insert_bars(market, "1321", [100.0] * 65 + [150.0], end=_ASOF)
    _insert_bars(market, "1234", [100.0] * 65 + [150.0], end=_ASOF)
    _calendar(market, {_ASOF: "1"})
    now = datetime(2026, 5, 29, 18, tzinfo=JST)
    if basis == "malformed":
        listing.write_bytes(b'{"JP":"private-source-detail"}')
    elif basis == "invalid_row":
        _current_listing(listing, code="1234 ")
    elif basis == "future_receipt":
        _current_listing(listing, received_at=now + timedelta(microseconds=1))
    elif basis == "wrong_session":
        _current_listing(listing, session=_ASOF - timedelta(days=1))

    with patch("baibai_engine.cli.market_snapshot.datetime") as clock:
        clock.now.return_value = now
        assert (
            engine_cli(
                [
                    "market-context",
                    "snapshot",
                    "--sqlite-path",
                    str(market),
                    "--asof",
                    _ASOF.isoformat(),
                    "--weeks",
                    "2",
                    "--format",
                    "json",
                    *(["--current-listing-master", str(listing)] if basis != "omitted" else []),
                ]
            )
            == 0
        )
    result = capsys.readouterr()
    payload = json.loads(result.out)
    assert payload["master_snapshot_date"] is None
    assert payload["sectors"] == []
    assert [point["breadth_sample_size"] for point in payload["points"]] == [0, 0]
    assert all(point["breadth_pct_above_ma20"] is None for point in payload["points"])
    assert payload["points"][-1]["benchmark_return_20d"] == 0.5
    assert ("breadth and sectors unavailable" in result.err) == (
        basis in {"missing", "malformed", "invalid_row"}
    )
    assert "private-source-detail" not in result.err
    assert str(listing) not in result.err


@pytest.mark.parametrize("lagging_prices", [False, True])
def test_public_current_snapshot_on_verified_closure_never_backcasts_membership(
    tmp_path, capsys, lagging_prices
):
    market = tmp_path / "market.sqlite"
    listing = tmp_path / "current.json"
    weekend = _ASOF + timedelta(days=2)
    end = _ASOF - timedelta(days=lagging_prices)
    _insert_bars(market, "1321", [100.0] * 65 + [150.0], end=end)
    _insert_bars(market, "1234", [100.0] * 65 + [150.0], end=end)
    _calendar(market, {_ASOF: "1", weekend - timedelta(days=1): "0", weekend: "3"})
    _current_listing(listing)

    now = datetime(2026, 5, 31, 18, tzinfo=JST)
    with patch("baibai_engine.cli.market_snapshot.datetime") as clock:
        clock.now.return_value = now
        assert (
            engine_cli(
                [
                    "market-context",
                    "snapshot",
                    "--sqlite-path",
                    str(market),
                    "--asof",
                    weekend.isoformat(),
                    "--weeks",
                    "2",
                    "--format",
                    "json",
                    "--current-listing-master",
                    str(listing),
                ]
            )
            == 0
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["as_of"] == weekend.isoformat()
    assert payload["master_snapshot_date"] == _ASOF.isoformat()
    assert payload["points"][0]["breadth_sample_size"] == 0
    active = payload["points"][-1]
    assert active["observation_date"] == end.isoformat()
    assert active["benchmark_return_20d"] == 0.5
    if lagging_prices:
        assert active["breadth_sample_size"] == 0
        assert payload["sectors"] == []
    else:
        assert active["breadth_sample_size"] == 1
        (sector,) = payload["sectors"]
        assert sector["sector_33"] == "建設業"
        assert sector["pct_above_ma20"] == 1
