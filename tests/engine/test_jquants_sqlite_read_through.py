from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.screening_sqlite import add_source_coverage, make_master_records

from baibai_engine.market.sqlite import range_covered
from baibai_engine.screening.providers.jquants import JQuantsProvider, JQuantsProviderError
from baibai_engine.screening.sqlite_cache import (
    open_connection,
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_master,
)
from baibai_engine.screening.sqlite_coverage import plan_required_field_repair


class _RecordingClient:
    """Stand-in for jquantsapi.ClientV2 that records every call so tests can
    confirm the SQLite read-through path skipped the API entirely.
    """

    def __init__(self) -> None:
        self.eq_master_calls: list[str] = []
        self.bars_calls: list[tuple[str, str]] = []
        self.fin_calls: list[tuple[str, str]] = []

    def get_eq_master(self, *, date: str) -> list[dict[str, Any]]:
        self.eq_master_calls.append(date)
        records = make_master_records(dt.date.fromisoformat(date))
        records[0]["CoName"] = "API_FALLBACK_NAME"
        return records

    def get_eq_bars_daily_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
        self.bars_calls.append((start_dt, end_dt))
        # Return a dense bar per day so a fetched chunk satisfies the data-derived
        # coverage check (every trading day present).
        current = date.fromisoformat(start_dt)
        end = date.fromisoformat(end_dt)
        bars: list[dict[str, Any]] = []
        while current <= end:
            bars.append(
                {"Code": "13010", "Date": f"{current.isoformat()}T00:00:00", "C": 999.0, "Va": 0.0}
            )
            current += timedelta(days=1)
        return bars

    def get_fin_summary_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
        self.fin_calls.append((start_dt, end_dt))
        return []


def _add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    record_count: int,
    min_date: str,
    max_date: str,
    path: str | None = None,
) -> None:
    add_source_coverage(
        conn,
        source=source,
        coverage_key=path or f"sqlite:{source}",
        record_count=record_count,
        min_date=min_date,
        max_date=max_date,
    )


def _insert_daily_bars(
    conn: sqlite3.Connection, ranges: list[tuple[str, str]], *, close: float = 999.0
) -> None:
    for start, end in ranges:
        current = date.fromisoformat(start)
        stop = date.fromisoformat(end)
        while current <= stop:
            conn.execute(
                "INSERT INTO jquants_daily_bars("
                "ticker, traded_at, close, turnover_value, adjustment_close"
                ") VALUES (?, ?, ?, ?, ?)",
                ("1301", current.isoformat(), close, 1000.0, close),
            )
            current += timedelta(days=1)


class JQuantsProviderSQLiteReadThroughTests(unittest.TestCase):
    def test_retry_waits_between_calls_and_not_after_final_failure(self) -> None:
        events: list[object] = []

        def always_retryable(**_params: object) -> object:
            events.append("call")
            raise RuntimeError("Too Many Requests")

        provider = JQuantsProvider("token", Path(".cache"), client=object())
        with (
            patch(
                "baibai_engine.market.provider.time.sleep",
                side_effect=lambda seconds: events.append(seconds),
            ),
            self.assertRaises(JQuantsProviderError),
        ):
            provider._call_with_retry("get_eq_master", always_retryable, date="2026-08-01")

        expected: list[object] = []
        for delay in provider._RATE_LIMIT_BACKOFF_SECONDS:
            expected.extend(("call", delay))
        expected.append("call")
        self.assertEqual(events, expected)

    def test_non_retryable_failure_returns_without_sleep(self) -> None:
        calls = 0

        def fail_once(**_params: object) -> object:
            nonlocal calls
            calls += 1
            raise ValueError("invalid request")

        provider = JQuantsProvider("token", Path(".cache"), client=object())
        with (
            patch("baibai_engine.market.provider.time.sleep") as sleep,
            self.assertRaises(JQuantsProviderError),
        ):
            provider._call_with_retry("get_eq_master", fail_once, date="2026-08-01")

        self.assertEqual(calls, 1)
        sleep.assert_not_called()

    def test_get_eq_master_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            asof = date(2026, 5, 7)
            records = make_master_records(asof)
            records[0]["CoName"] = "極洋"
            store_jquants_master(sqlite_path, records, requested_asof=asof)

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            masters = provider.get_eq_master(asof)

            self.assertEqual(client.eq_master_calls, [])
            self.assertEqual(len(masters), 2500)
            self.assertEqual(masters[0].name, "極洋")

    def test_get_eq_master_falls_back_to_api_when_sqlite_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            asof = date(2026, 5, 7)
            masters = provider.get_eq_master(asof)

            self.assertEqual(client.eq_master_calls, ["2026-05-07"])
            self.assertEqual(masters[0].name, "API_FALLBACK_NAME")

    def test_get_bars_uses_sqlite_for_covered_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            # Dense bars across the whole window: coverage is derived from the
            # rows, so every trading day must be present to skip the API.
            _insert_daily_bars(conn, [("2024-03-19", "2024-04-18")], close=3790.0)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=31,
                min_date="2024-03-19",
                max_date="2024-04-18",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            bars = provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 18))

            self.assertEqual(client.bars_calls, [])
            self.assertGreater(len(bars), 1)
            self.assertEqual(bars[0].close, 3790.0)

    def test_get_fin_summaries_uses_persisted_chunk_without_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            disclosed = date(2024, 4, 18)
            store_jquants_fin_summaries(
                sqlite_path,
                [{"Code": "13010", "DisclosedDate": disclosed.isoformat(), "NetSales": 100}],
                requested_start=disclosed,
                requested_end=disclosed,
            )

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            summaries = provider.get_fin_summary_range(disclosed, disclosed)

            self.assertEqual(client.fin_calls, [])
            self.assertEqual(len(summaries), 1)

    def test_normalized_profit_inputs_read_only_fy_rows_and_split_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            start = date(2021, 1, 1)
            end = date(2024, 4, 18)
            conn = open_connection(sqlite_path)
            _insert_daily_bars(conn, [(start.isoformat(), end.isoformat())])
            conn.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor = ? WHERE traded_at = ?",
                (2.0, "2023-10-02"),
            )
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=(end - start).days + 1,
                min_date=start.isoformat(),
                max_date=end.isoformat(),
            )
            conn.executemany(
                "INSERT INTO jquants_fin_summaries("
                "ticker, disclosed_at, eps_ttm, fiscal_period, fiscal_year_end"
                ") VALUES (?, ?, ?, ?, ?)",
                [
                    ("1301", "2022-05-15", 10.0, "FY", "2022-03-31"),
                    ("1301", "2023-05-15", 20.0, "FY", "2023-03-31"),
                    ("1301", "2024-02-01", 25.0, "3Q", "2024-03-31"),
                    ("1301", "2024-04-15", 30.0, "FY", "2024-03-31"),
                ],
            )
            _add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                record_count=4,
                min_date=start.isoformat(),
                max_date=end.isoformat(),
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            split_bars = provider.get_adjustment_factor_bars_range(start, end)
            fy_summaries = provider.get_fy_summary_range(start, end)

            self.assertEqual(client.bars_calls, [])
            self.assertEqual(client.fin_calls, [])
            self.assertEqual(
                [(bar.traded_at.isoformat(), bar.adjustment_factor) for bar in split_bars],
                [("2023-10-02", 2.0)],
            )
            self.assertEqual([row.eps_ttm for row in fy_summaries], [10.0, 20.0, 30.0])

    def test_get_bars_refetches_recent_tail_when_asof_ahead_of_cache(self) -> None:
        """An incremental asof a few days ahead of the cached tail looks covered
        (the data-derived check tolerates a holiday-sized edge gap), but a
        fetch-capable run must still pull the new days through the asof so a
        weekly/daily bootstrap does not silently skip the latest trading day."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _insert_daily_bars(conn, [("2024-04-01", "2024-04-15")])
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            provider.get_eq_bars_daily_range(date(2024, 4, 1), date(2024, 4, 18))

            self.assertTrue(any(end == "2024-04-18" for _, end in client.bars_calls))
            conn = sqlite3.connect(sqlite_path)
            try:
                latest = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(latest, "2024-04-18")

    def test_a_window_ending_behind_the_store_does_not_refetch_its_tail(self) -> None:
        """A historical window whose end falls on a closed market looks covered with
        an edge gap, but nothing inside it can have been published since the last
        fetch. Reading it live would delete and rewrite that cross-section on every
        pass, and a provider that answered with nothing would erase it."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _insert_daily_bars(conn, [("2024-04-01", "2024-04-15"), ("2024-06-03", "2024-06-14")])
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            bars = provider.get_eq_bars_daily_range(date(2024, 4, 1), date(2024, 4, 18))

            self.assertEqual(client.bars_calls, [])
            self.assertGreater(len(bars), 1)

    def test_cache_only_does_not_refetch_tail_behind_asof(self) -> None:
        """A cache-only screening run trusts the validated coverage and never
        fetches, even when the cached tail is a few days behind the asof."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _insert_daily_bars(conn, [("2024-04-01", "2024-04-15")])
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider(
                "token", cache_dir, client=client, sqlite_path=sqlite_path, cache_only=True
            )

            bars = provider.get_eq_bars_daily_range(date(2024, 4, 1), date(2024, 4, 18))

            self.assertEqual(client.bars_calls, [])
            self.assertGreater(len(bars), 1)

    def test_get_bars_falls_back_when_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=0,
                min_date="2024-04-01",
                max_date="2024-04-10",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            # Range starts before imported window, so fall through to API/JSON.
            provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 5))

            self.assertGreater(len(client.bars_calls), 0)

    def test_cache_only_raises_when_bars_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=0,
                min_date="2024-04-01",
                max_date="2024-04-10",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider(
                "token",
                cache_dir,
                client=client,
                sqlite_path=sqlite_path,
                cache_only=True,
            )

            with self.assertRaisesRegex(JQuantsProviderError, "SQLite cache incomplete"):
                provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 5))

            self.assertEqual(client.bars_calls, [])

    def test_get_bars_falls_back_when_imported_chunks_have_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            # Dense at both ends but a hole in the middle wider than any market
            # closure (2024-03-22..2024-04-14), so the data-derived check sees the
            # window as not covered and the provider refetches.
            _insert_daily_bars(conn, [("2024-03-19", "2024-03-21"), ("2024-04-15", "2024-04-18")])
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=3,
                min_date="2024-03-19",
                max_date="2024-03-21",
                path=(
                    "records/_data/raw/screening/jquants/"
                    "get_eq_bars_daily_range-end_dt-2024-03-21-start_dt-2024-03-19.json"
                ),
            )
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                record_count=4,
                min_date="2024-04-15",
                max_date="2024-04-18",
                path=(
                    "records/_data/raw/screening/jquants/"
                    "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-04-15.json"
                ),
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 18))

            self.assertGreater(len(client.bars_calls), 0)

    def test_get_bars_fetches_only_missing_sqlite_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            # Dense bars for the first chunk so the data-derived check recognizes
            # it as covered and the provider only fetches the later chunks.
            first_chunk: list[dict[str, object]] = []
            current = date(2024, 3, 19)
            while current <= date(2024, 4, 18):
                first_chunk.append(
                    {"Code": "13010", "Date": current.isoformat(), "C": 3790.0, "Va": 1000.0}
                )
                current += timedelta(days=1)
            store_jquants_daily_bars(
                sqlite_path,
                first_chunk,
                requested_start=date(2024, 3, 19),
                requested_end=date(2024, 4, 18),
            )

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            with patch("baibai_engine.market.provider.time.sleep") as sleep:
                bars = provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 5, 20))

            self.assertNotIn(("2024-03-19", "2024-04-18"), client.bars_calls)
            self.assertIn(("2024-04-19", "2024-05-19"), client.bars_calls)
            self.assertIn(("2024-05-20", "2024-05-20"), client.bars_calls)
            self.assertGreaterEqual(len(bars), 3)
            sleep.assert_called_once_with(3.0)


class _FinRecordingClient(_RecordingClient):
    """Answers summary ranges with a row, so a fetch actually establishes coverage.

    The shared fake returns nothing, which records a zero-count window and leaves
    the range still unreadable — fine where the fetch is not the subject, useless
    for asking what the planner requested.
    """

    def get_fin_summary_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
        self.fin_calls.append((start_dt, end_dt))
        return [
            {
                "Code": "13010",
                "DisclosedDate": start_dt,
                "NetSales": 100,
                "TypeOfCurrentPeriod": "FY",
            }
        ]


class FinSummaryFetchWindowTests(unittest.TestCase):
    """What a summaries fetch asks J-Quants for, given what the store already holds.

    The window the caller passes is anchored on the as-of and slides daily, so a
    planner that walked it would re-read up to a month to add a day. These pin the
    planner to the store's own coverage instead.
    """

    _START = date(2024, 5, 1)
    _END = date(2026, 5, 1)

    def _seed_coverage(self, sqlite_path: Path, windows: list[tuple[date, date, bool]]) -> None:
        """Record coverage windows, with rows only where the window claims some.

        A window recorded `ok` while holding nothing is a real state — a trim that
        recounted to zero leaves exactly that — so it is seeded without rows.
        """
        conn = open_connection(sqlite_path)
        for index, (low, high, has_rows) in enumerate(windows):
            if has_rows:
                conn.execute(
                    "INSERT INTO jquants_fin_summaries("
                    "ticker, disclosed_at, eps_ttm, fiscal_period, fiscal_year_end"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (f"130{index}", low.isoformat(), 10.0, "FY", high.isoformat()),
                )
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=f"get_fin_summary_range:{low.isoformat()}..{high.isoformat()}",
                record_count=1 if has_rows else 0,
                min_date=low.isoformat(),
                max_date=high.isoformat(),
            )
        conn.commit()
        conn.close()

    def _requested(self, windows: list[tuple[date, date, bool]]) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            self._seed_coverage(sqlite_path, windows)
            client = _FinRecordingClient()
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=client, sqlite_path=sqlite_path
            )
            with patch("baibai_engine.market.provider.time.sleep"):
                provider.get_fin_summary_range(self._START, self._END)
            return client.fin_calls

    def test_a_fully_covered_window_is_not_requested_at_all(self) -> None:
        self.assertEqual(self._requested([(self._START, self._END, True)]), [])

    def test_an_empty_store_requests_the_whole_window(self) -> None:
        """Nothing held means nothing skipped; the chunking is unchanged.

        The request is still split into rate-limit-sized chunks, so what matters is
        that they tile the window end to end with no day left out.
        """
        requested = self._requested([])

        self.assertEqual(requested[0][0], self._START.isoformat())
        self.assertEqual(requested[-1][1], self._END.isoformat())
        for (_, earlier_end), (later_start, _) in pairwise(requested):
            self.assertEqual(
                date.fromisoformat(later_start),
                date.fromisoformat(earlier_end) + timedelta(days=1),
            )

    def test_one_stale_day_requests_one_day(self) -> None:
        self.assertEqual(
            self._requested([(self._START, self._END - timedelta(days=1), True)]),
            [("2026-05-01", "2026-05-01")],
        )

    def test_a_ten_day_stop_requests_exactly_those_ten_days(self) -> None:
        self.assertEqual(
            self._requested([(self._START, self._END - timedelta(days=10), True)]),
            [("2026-04-22", "2026-05-01")],
        )

    def test_abutting_windows_leave_nothing_to_request(self) -> None:
        boundary = date(2025, 6, 1)
        self.assertEqual(
            self._requested(
                [
                    (self._START, boundary, True),
                    (boundary + timedelta(days=1), self._END, True),
                ]
            ),
            [],
        )

    def test_a_gap_between_windows_requests_only_the_gap(self) -> None:
        boundary = date(2025, 6, 1)
        self.assertEqual(
            self._requested(
                [
                    (self._START, boundary, True),
                    (boundary + timedelta(days=6), self._END, True),
                ]
            ),
            [("2025-06-02", "2025-06-06")],
        )

    def test_a_window_recorded_ok_with_no_rows_is_requested_again(self) -> None:
        """`_trim_ok_coverage_around` leaves these behind on purpose.

        `range_covered` refuses to read through a zero-count window, so a planner
        that counted it as held would skip the fetch and then find the reader still
        unable to serve the range — every run, with no way to recover.
        """
        boundary = date(2025, 6, 1)
        self.assertEqual(
            self._requested(
                [
                    (self._START, boundary, True),
                    (boundary + timedelta(days=1), date(2025, 6, 20), False),
                    (date(2025, 6, 21), self._END, True),
                ]
            ),
            [("2025-06-02", "2025-06-20")],
        )

    def test_the_head_is_requested_when_coverage_starts_late(self) -> None:
        self.assertEqual(
            self._requested([(self._START + timedelta(days=30), self._END, True)]),
            [("2024-05-01", "2024-05-30")],
        )

    def _refreshed(
        self, windows: list[tuple[date, date, bool]], *, overlap: int
    ) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            self._seed_coverage(sqlite_path, windows)
            client = _FinRecordingClient()
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=client, sqlite_path=sqlite_path
            )
            with patch("baibai_engine.market.provider.time.sleep"):
                provider.refresh_fin_summary_range(
                    self._START, self._END, revision_overlap_days=overlap
                )
            return client.fin_calls

    def test_a_refresh_rereads_the_trailing_window_even_when_covered(self) -> None:
        """The overlap is the only path by which a late-published filing lands."""
        self.assertEqual(
            self._refreshed([(self._START, self._END, True)], overlap=7),
            [("2026-04-24", "2026-05-01")],
        )

    def test_a_refresh_merges_the_overlap_with_an_adjacent_gap(self) -> None:
        self.assertEqual(
            self._refreshed([(self._START, self._END - timedelta(days=1), True)], overlap=7),
            [("2026-04-24", "2026-05-01")],
        )

    def test_a_stop_longer_than_the_overlap_is_fetched_whole_and_once(self) -> None:
        """The gap decides the width; the overlap never truncates it, nor repeats it."""
        self.assertEqual(
            self._refreshed([(self._START, self._END - timedelta(days=30), True)], overlap=7),
            [("2026-04-02", "2026-05-01")],
        )

    def test_a_refresh_never_asks_for_the_same_range_twice(self) -> None:
        """A gap away from the tail leaves two ranges; neither may repeat the other."""
        boundary = date(2025, 6, 1)
        requested = self._refreshed(
            [
                (self._START, boundary, True),
                (boundary + timedelta(days=6), self._END - timedelta(days=1), True),
            ],
            overlap=7,
        )

        self.assertEqual(len(requested), len(set(requested)))
        self.assertEqual(requested, [("2025-06-02", "2025-06-06"), ("2026-04-24", "2026-05-01")])

    def test_a_refresh_paces_between_two_separate_ranges(self) -> None:
        """Two ranges either side of a gap are still two requests to one API."""
        boundary = date(2025, 6, 1)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            self._seed_coverage(
                sqlite_path,
                [
                    (self._START, boundary, True),
                    (boundary + timedelta(days=6), self._END - timedelta(days=1), True),
                ],
            )
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=_FinRecordingClient(), sqlite_path=sqlite_path
            )

            with patch("baibai_engine.market.provider.time.sleep") as sleep:
                provider.refresh_fin_summary_range(self._START, self._END, revision_overlap_days=7)

            sleep.assert_called_once_with(3.0)

    def test_repair_chunks_commit_and_report_progress_before_a_later_chunk_fails(self) -> None:
        """A retry can re-plan from the committed first chunk instead of starting over."""
        first_repair = self._START + timedelta(days=100)
        second_repair = self._START + timedelta(days=200)
        tickers = tuple(f"{1301 + index:04d}" for index in range(100))
        first_tickers = tickers[:40]

        class _FailingSecondRepairClient(_RecordingClient):
            def get_fin_summary_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
                self.fin_calls.append((start_dt, end_dt))
                if start_dt == second_repair.isoformat():
                    raise ValueError("interrupted")
                return [
                    {
                        "Code": f"{ticker}0",
                        "DisclosedDate": first_repair.isoformat(),
                        "ShOutFY": 10_000_000,
                        "TrShFY": 1_000_000,
                        "EqAR": 0.5,
                    }
                    for ticker in first_tickers
                ]

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.executemany(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, is_common_stock) VALUES (?, ?, 1)",
                [(self._END.isoformat(), ticker) for ticker in tickers],
            )
            conn.executemany(
                "INSERT INTO jquants_fin_summaries("
                "ticker, disclosed_at, shares_outstanding, treasury_shares, "
                "equity_to_asset_ratio) VALUES (?, ?, ?, ?, ?)",
                [(ticker, first_repair.isoformat(), None, None, None) for ticker in first_tickers]
                + [
                    (ticker, second_repair.isoformat(), None, None, None)
                    for ticker in tickers[40:80]
                ]
                + [
                    (ticker, self._END.isoformat(), 10_000_000.0, 1_000_000.0, 0.5)
                    for ticker in tickers[80:]
                ],
            )
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=f"get_fin_summary_range:{self._START}..{self._END}",
                record_count=100,
                min_date=self._START.isoformat(),
                max_date=self._END.isoformat(),
            )
            conn.commit()
            conn.close()
            initial = plan_required_field_repair(sqlite_path, start=self._START, asof=self._END)
            self.assertIsNotNone(initial)
            assert initial is not None
            self.assertEqual(
                initial.ranges,
                ((first_repair, first_repair), (second_repair, second_repair)),
            )
            client = _FailingSecondRepairClient()
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=client, sqlite_path=sqlite_path
            )
            progress: list[tuple[int, int, date, date]] = []

            with (
                patch("baibai_engine.market.provider.time.sleep"),
                self.assertRaisesRegex(JQuantsProviderError, "interrupted"),
            ):
                provider.refresh_fin_summary_range(
                    self._START,
                    self._END,
                    revision_overlap_days=0,
                    repair_ranges=initial.ranges,
                    progress=lambda index, total, low, high: progress.append(
                        (index, total, low, high)
                    ),
                )

            self.assertEqual(
                progress,
                [
                    (1, 3, first_repair, first_repair),
                ],
            )
            resumed = plan_required_field_repair(sqlite_path, start=self._START, asof=self._END)
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed.ranges, ((second_repair, second_repair),))
            conn = sqlite3.connect(sqlite_path)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM jquants_fin_summaries "
                        "WHERE disclosed_at = ? AND shares_outstanding IS NOT NULL "
                        "AND treasury_shares IS NOT NULL AND equity_to_asset_ratio IS NOT NULL",
                        (first_repair.isoformat(),),
                    ).fetchone(),
                    (40,),
                )
                self.assertTrue(
                    range_covered(conn, "jquants_fin_summaries", self._START, self._END)
                )
            finally:
                conn.close()

    def test_a_refresh_then_a_normalized_read_covers_both_windows(self) -> None:
        """Bootstrap asks for a 730-day window and a 2,200-day one from this source.

        Narrowing the requests must still leave `verify-cache-coverage` satisfied on
        both, or the batch stops right after the fetch that was supposed to fix it.
        """
        normalized_start = self._END - timedelta(days=2200)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            open_connection(sqlite_path).close()
            client = _FinRecordingClient()
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=client, sqlite_path=sqlite_path
            )

            with patch("baibai_engine.market.provider.time.sleep"):
                provider.refresh_fin_summary_range(self._START, self._END, revision_overlap_days=7)
                provider.get_fy_summary_range(normalized_start, self._END)

            conn = sqlite3.connect(sqlite_path)
            try:
                self.assertTrue(
                    range_covered(conn, "jquants_fin_summaries", self._START, self._END)
                )
                self.assertTrue(
                    range_covered(conn, "jquants_fin_summaries", normalized_start, self._END)
                )
            finally:
                conn.close()


class CachedRangeInspectionCostTests(unittest.TestCase):
    """Deciding what to fetch must not cost a model per stored row.

    The bar window is over three million rows in production and the decision is a
    boolean; building the rows to reach it is what made a warm bootstrap spend
    ~26s reading data it already had.
    """

    def _covered_bar_store(self, tmp: str) -> Path:
        sqlite_path = Path(tmp) / "cache" / "market.sqlite"
        conn = open_connection(sqlite_path)
        _insert_daily_bars(conn, [("2024-03-19", "2024-04-18")])
        conn.commit()
        conn.close()
        return sqlite_path

    def test_ensuring_a_covered_bar_range_builds_no_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = self._covered_bar_store(tmp)
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=_RecordingClient(), sqlite_path=sqlite_path
            )

            with patch("baibai_engine.market.provider.read_daily_bars", side_effect=AssertionError):
                rows = provider.ensure_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 18))

            self.assertEqual(rows, 31)

    def test_skipping_cached_bar_chunks_builds_no_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = self._covered_bar_store(tmp)
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=_RecordingClient(), sqlite_path=sqlite_path
            )

            with (
                patch("baibai_engine.market.provider.read_daily_bars", side_effect=AssertionError),
                patch("baibai_engine.market.provider.time.sleep"),
            ):
                provider._fetch_missing_range_chunks(
                    "get_eq_bars_daily_range", date(2024, 3, 19), date(2024, 5, 20)
                )

    def test_skipping_cached_summary_chunks_builds_no_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            store_jquants_fin_summaries(
                sqlite_path,
                [{"Code": "13010", "DisclosedDate": "2024-04-18", "NetSales": 100}],
                requested_start=date(2024, 4, 1),
                requested_end=date(2024, 4, 30),
            )
            provider = JQuantsProvider(
                "token", Path(tmp) / "raw", client=_FinRecordingClient(), sqlite_path=sqlite_path
            )

            with (
                patch(
                    "baibai_engine.screening.sqlite_reader.read_fin_summaries",
                    side_effect=AssertionError,
                ),
                patch("baibai_engine.market.provider.time.sleep"),
            ):
                provider._fetch_missing_range_chunks(
                    "get_fin_summary_range", date(2024, 4, 1), date(2024, 5, 31)
                )


class JQuantsProviderMiscTests(unittest.TestCase):
    def test_provider_works_when_sqlite_path_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _RecordingClient()
            provider = JQuantsProvider("token", Path(tmp), client=client)

            masters = provider.get_eq_master(date(2026, 5, 7))

            self.assertEqual(client.eq_master_calls, ["2026-05-07"])
            self.assertEqual(masters[0].name, "API_FALLBACK_NAME")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
