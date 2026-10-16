from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.market.jpx_models import JPXEarningsCalendarEntry, JPXEarningsCalendarSnapshot
from baibai_engine.market.jquants_decode import JQuantsProviderError, parse_jquants_code_parts
from baibai_engine.market.providers.jquants import JQuantsProvider
from baibai_engine.storage.observations.convert import code_quality, to_float, to_str_or_none
from baibai_engine.storage.observations.coverage import (
    EmptyRangeReplacementError,
    covered_intervals,
    daily_bars_covered_by_data,
)
from baibai_engine.storage.observations.ingest.edinet import (
    store_edinet_documents,
    store_edinet_metrics,
)
from baibai_engine.storage.observations.ingest.jpx import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.storage.observations.ingest.jquants import (
    store_jquants_all_issues_daily_margin,
    store_jquants_fin_summaries,
    store_jquants_margin_alerts,
    store_jquants_master,
    store_jquants_weekly_margin,
)
from baibai_engine.storage.observations.jquants import (
    store_jquants_daily_bars,
    store_jquants_market_calendar,
)
from baibai_engine.storage.observations.reader import (
    all_issues_daily_margin_backfill_candidate_dates,
    all_issues_daily_margin_candidate_dates,
    final_legacy_week_requires_refresh,
    published_margin_week_ends,
    range_covered,
    read_all_issues_daily_margin,
    read_fin_summaries,
    read_margin_alerts,
    read_weekly_margin,
    weekly_margin_candidate_dates,
    weekly_margin_empty_requires_refresh,
)
from baibai_engine.storage.observations.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
)


def _earnings_snapshot(on_date: date = date(2026, 5, 15)) -> JPXEarningsCalendarSnapshot:
    return JPXEarningsCalendarSnapshot(
        entries=(JPXEarningsCalendarEntry(ticker="7203", announcement_date=on_date),),
        source_urls=("https://www.jpx.co.jp/kessan.xlsx",),
        raw_record_count=2,
        excluded_record_count=1,
        superseded_record_count=0,
    )


class SQLiteCacheTest(unittest.TestCase):
    def test_source_code_parser_distinguishes_primary_and_secondary_issues(self) -> None:
        for raw, expected in [
            (" 7203 ", ("7203", "ok")),
            ("130A0", ("130A", "ok")),
            ("72031", (None, "excluded")),
            ("123", (None, "rejected")),
            ("12-3", (None, "rejected")),
            ("１２３４", (None, "rejected")),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(code_quality(raw), expected)
        self.assertEqual(parse_jquants_code_parts("130a0"), ("130A", True))
        self.assertEqual(parse_jquants_code_parts("72031"), ("7203", False))
        # Every malformed source code uses the provider error boundary.
        for raw in ("123", "12-3", "１２３４"):
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(JQuantsProviderError, "invalid J-Quants code"),
            ):
                parse_jquants_code_parts(raw)

    def test_sqlite_scalar_missing_markers_preserve_zero(self) -> None:
        for value in (None, "", " NaT ", "nan", "null", "<NA>", float("nan"), pd.NA, pd.NaT):
            with self.subTest(value=value):
                self.assertIsNone(to_float(value))
                self.assertIsNone(to_str_or_none(value))
        self.assertEqual(to_float(0), 0.0)
        self.assertEqual(to_str_or_none(0), "0")

    def test_creates_current_schema_with_user_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
            finally:
                conn.close()

            self.assertEqual(version[0], SQLITE_SCHEMA_VERSION)
            self.assertIn("source_coverage", tables)
            self.assertNotIn("raw_imports", tables)
            self.assertNotIn("cache_metadata", tables)

    def test_rejects_legacy_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE source_coverage(source TEXT)")
                conn.execute("PRAGMA user_version = 9")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(SQLiteSchemaError, "obsolete market SQLite schema"):
                open_connection(db)

    def test_direct_store_writes_minimal_source_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_daily_bars(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "C": 1000,
                        "AdjC": 1000,
                    }
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            self.assertEqual(rows, 1)
            conn = sqlite3.connect(db)
            try:
                coverage = conn.execute(
                    "SELECT source, coverage_key, record_count, status FROM source_coverage"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                coverage,
                (
                    "jquants_daily_bars",
                    "get_eq_bars_daily_range:2026-05-08..2026-05-08",
                    1,
                    "ok",
                ),
            )

    def test_daily_bars_source_coverage_counts_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_daily_bars(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "C": 1000,
                        "AdjC": 1000,
                    },
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "C": 1000,
                        "AdjC": 1000,
                    },
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            conn = sqlite3.connect(db)
            try:
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_daily_bars",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(rows, 1)
            self.assertEqual(table_count[0], 1)
            self.assertEqual(coverage, (1,))

    def test_fin_summaries_source_coverage_counts_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_fin_summaries(
                db,
                [
                    {
                        "Code": "72030",
                        "DiscDate": "2026-05-08",
                        "Sales": 100,
                        "DiscNo": "synthetic-document",
                    },
                    {
                        "Code": "72030",
                        "DiscDate": "2026-05-08",
                        "Sales": 100,
                        "DiscNo": "synthetic-document",
                    },
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            conn = sqlite3.connect(db)
            try:
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_fin_summaries",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(rows, 1)
            self.assertEqual(table_count[0], 1)
            self.assertEqual(coverage, (1,))

    def test_fin_summaries_do_not_alias_average_shares_to_gross_issued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-41670-2024-08-14",
                        "Code": "41670",
                        "DiscDate": "2024-08-14",
                        "AvgSh": "7563857",
                        "TrShFY": "352373",
                    }
                ],
                requested_start=date(2024, 8, 14),
                requested_end=date(2024, 8, 14),
            )
            conn = sqlite3.connect(db)
            try:
                row = conn.execute("SELECT document_payload FROM jquants_fin_summaries").fetchone()
            finally:
                conn.close()

            raw = json.loads(row[0])
            self.assertNotIn("ShOutFY", raw)
            self.assertEqual((raw["AvgSh"], raw["TrShFY"]), ("7563857", "352373"))
            summaries = read_fin_summaries(db, date(2024, 8, 14), date(2024, 8, 14))
            assert summaries is not None
            self.assertIsNone(summaries[0].shares_outstanding)
            self.assertEqual(summaries[0].average_shares, 7_563_857.0)

    def test_fin_summaries_retain_both_forecast_slots_without_selecting(self) -> None:
        # Period selection belongs to L2; storage keeps both provider slots intact.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-97150-2026-04-30",
                        "Code": "97150",
                        "DiscDate": "2026-04-30",
                        "FEPS": "",
                        "NxFEPS": "360.26",
                    },
                    {
                        "DiscNo": "synthetic-72030-2026-05-13",
                        "Code": "72030",
                        "DiscDate": "2026-05-13",
                        "FEPS": "306.89",
                        "NxFEPS": "",
                    },
                ],
                requested_start=date(2026, 4, 30),
                requested_end=date(2026, 5, 13),
            )
            conn = sqlite3.connect(db)
            try:
                stored = dict(
                    conn.execute(
                        "SELECT ticker, document_payload FROM jquants_fin_summaries"
                    ).fetchall()
                )
            finally:
                conn.close()
            first, second = json.loads(stored["9715"]), json.loads(stored["7203"])
            self.assertEqual((first["FEPS"], first["NxFEPS"]), ("", "360.26"))
            self.assertEqual((second["FEPS"], second["NxFEPS"]), ("306.89", ""))

    def test_non_finite_current_forecasts_do_not_cross_into_next_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-97150-2026-05-13",
                        "Code": "97150",
                        "DiscDate": "2026-05-13",
                        "CurFYSt": "2026-04-01",
                        "CurFYEn": "2027-03-31",
                        "NxtFYSt": "2027-04-01",
                        "NxtFYEn": "2028-03-31",
                        "FEPS": float("nan"),
                        "NxFEPS": "120.0",
                        "FNP": float("inf"),
                        "NxFNp": "2000000000",
                        "FOdP": float("nan"),
                        "NxFOdP": "1800000000",
                        "FDivAnn": float("-inf"),
                        "NxFDivAnn": "12.0",
                        "Sales": float("inf"),
                        "CFO": float("-inf"),
                        "TA": float("nan"),
                        "ShOutFY": float("inf"),
                        "TrShFY": float("-inf"),
                        "EqAR": float("nan"),
                    }
                ],
                requested_start=date(2026, 5, 13),
                requested_end=date(2026, 5, 13),
            )
            summaries = read_fin_summaries(db, date(2026, 5, 13), date(2026, 5, 13))

            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertIsNone(summary.forecast_eps)
            self.assertIsNone(summary.forecast_profit)
            self.assertIsNone(summary.forecast_ordinary_profit)
            self.assertIsNone(summary.dps_forecast_annual)
            self.assertIsNone(summary.sales)
            self.assertIsNone(summary.cfo)
            self.assertIsNone(summary.total_assets)
            self.assertIsNone(summary.shares_outstanding)
            self.assertIsNone(summary.treasury_shares)
            self.assertIsNone(summary.equity_to_asset_ratio)

    def test_pandas_missing_current_forecasts_do_not_cross_periods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-97150-2026-05-13",
                        "Code": "97150",
                        "DiscDate": "2026-05-13",
                        "CurFYSt": "2026-04-01",
                        "CurFYEn": "2027-03-31",
                        "NxtFYSt": "2027-04-01",
                        "NxtFYEn": "2028-03-31",
                        "FEPS": pd.NA,
                        "NxFEPS": "120.0",
                        "FNP": pd.NA,
                        "NxFNp": "2000000000",
                        "FOdP": pd.NA,
                        "NxFOdP": "1800000000",
                        "FDivAnn": pd.NA,
                        "NxFDivAnn": "12.0",
                    }
                ],
                requested_start=date(2026, 5, 13),
                requested_end=date(2026, 5, 13),
            )
            summaries = read_fin_summaries(db, date(2026, 5, 13), date(2026, 5, 13))

            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertIsNone(summary.forecast_eps)
            self.assertIsNone(summary.forecast_profit)
            self.assertIsNone(summary.forecast_ordinary_profit)
            self.assertIsNone(summary.dps_forecast_annual)

    def test_fin_summaries_keep_zero_current_forecasts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-97150-2026-05-13",
                        "Code": "97150",
                        "DiscDate": "2026-05-13",
                        "CurFYSt": "2026-04-01",
                        "CurFYEn": "2027-03-31",
                        "NxtFYSt": "2027-04-01",
                        "NxtFYEn": "2028-03-31",
                        "FEPS": 0,
                        "NxFEPS": "120.0",
                        "FNP": "1000000000",
                        "NxFNp": "2000000000",
                        "FOdP": "900000000",
                        "NxFOdP": "1800000000",
                        "FDivAnn": 0,
                        "NxFDivAnn": "12.0",
                    }
                ],
                requested_start=date(2026, 5, 13),
                requested_end=date(2026, 5, 13),
            )
            summaries = read_fin_summaries(db, date(2026, 5, 13), date(2026, 5, 13))

            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertEqual(summary.forecast_eps, 0.0)
            self.assertEqual(summary.forecast_profit, 1_000_000_000.0)
            self.assertEqual(summary.forecast_ordinary_profit, 900_000_000.0)
            self.assertEqual(summary.dps_forecast_annual, 0.0)

    def test_fin_summaries_forecast_profit_pair_round_trips_by_period(self) -> None:
        # Storage retains both slots; the reader projects the applicable fiscal period.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-48490-2026-05-13",
                        "Code": "48490",
                        "DiscDate": "2026-05-13",
                        "CurFYSt": "2026-04-01",
                        "NxtFYSt": "2027-04-01",
                        "NxtFYEn": "2028-03-31",
                        "FEPS": "138.72",
                        "FNP": "5464000000",
                        "FOdP": "3406000000",
                        "NxFNp": "900000000",
                        "NxFOdP": "1200000000",
                        "CurPerType": "1Q",
                        "CurFYEn": "2027-03-31T00:00:00",
                        "CurPerSt": "2026-04-01T00:00:00",
                        "CurPerEn": "2026-06-30T00:00:00",
                    },
                    {
                        "DiscNo": "synthetic-97150-2026-04-30",
                        "Code": "97150",
                        "DiscDate": "2026-04-30",
                        "CurFYSt": "2025-04-01",
                        "CurFYEn": "2026-03-31",
                        "NxtFYSt": "2026-04-01",
                        "NxtFYEn": "2027-03-31",
                        "FEPS": "",
                        "NxFEPS": "360.26",
                        "FNP": "5464000000",
                        "FOdP": "3406000000",
                        "NxFNp": "2000000000",
                        "NxFOdP": "2500000000",
                    },
                ],
                requested_start=date(2026, 4, 30),
                requested_end=date(2026, 5, 13),
            )
            summaries = read_fin_summaries(db, date(2026, 4, 30), date(2026, 5, 13))
            assert summaries is not None
            by_ticker = {summary.ticker: summary for summary in summaries}
            self.assertEqual(by_ticker["4849"].forecast_profit, 5464000000.0)
            self.assertEqual(by_ticker["4849"].forecast_ordinary_profit, 3406000000.0)
            self.assertEqual(by_ticker["4849"].fiscal_period, "1Q")
            self.assertEqual(by_ticker["4849"].fiscal_year_end, date(2027, 3, 31))
            self.assertEqual(by_ticker["4849"].period_start, date(2026, 4, 1))
            self.assertEqual(by_ticker["4849"].period_end, date(2026, 6, 30))
            self.assertEqual(by_ticker["9715"].forecast_profit, 2000000000.0)
            self.assertEqual(by_ticker["9715"].forecast_ordinary_profit, 2500000000.0)

    def test_fin_summaries_shifted_chunk_refetch_keeps_coverage_contiguous(self) -> None:
        """A later bootstrap anchors chunk boundaries at a new asof, so a re-fetch only
        partially overlaps an existing window. Recording it must merge into the union,
        not delete-and-shrink the window and orphan its earlier part -- otherwise
        `range_covered` reports a gap even though the rows are still present.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-72030-2026-05-13",
                        "Code": "72030",
                        "DiscDate": "2026-05-13",
                        "Sales": 100,
                    },
                    {
                        "DiscNo": "synthetic-72040-2026-05-27",
                        "Code": "72040",
                        "DiscDate": "2026-05-27",
                        "Sales": 200,
                    },
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-72040-2026-05-27",
                        "Code": "72040",
                        "DiscDate": "2026-05-27",
                        "Sales": 200,
                    },
                    {
                        "DiscNo": "synthetic-72050-2026-06-02",
                        "Code": "72050",
                        "DiscDate": "2026-06-02",
                        "Sales": 300,
                    },
                ],
                requested_start=date(2026, 5, 19),
                requested_end=date(2026, 6, 5),
            )
            conn = sqlite3.connect(db)
            try:
                covered = range_covered(
                    conn, "jquants_fin_summaries", date(2026, 5, 12), date(2026, 6, 5)
                )
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, record_count FROM source_coverage "
                    "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_start"
                ).fetchall()
            finally:
                conn.close()
            self.assertTrue(covered)
            self.assertEqual(windows, [("2026-05-12", "2026-06-05", 3)])

    def test_partial_chunk_does_not_shrink_the_wider_window_it_lands_in(self) -> None:
        """One unusable record in one chunk must not retract the years of coverage
        around it. The chunk's own range stops being claimed so the quality problem
        stays visible, but the history outside it was never re-fetched and its claim
        still holds. Collapsing the window to the chunk leaves years of filings in the
        store unreadable behind a claim that no longer reaches them.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-72030-2021-08-02",
                        "Code": "72030",
                        "DiscDate": "2021-08-02",
                        "Sales": 100,
                    },
                    {
                        "DiscNo": "synthetic-72040-2024-01-10",
                        "Code": "72040",
                        "DiscDate": "2024-01-10",
                        "Sales": 200,
                    },
                    {
                        "DiscNo": "synthetic-72050-2026-07-16",
                        "Code": "72050",
                        "DiscDate": "2026-07-16",
                        "Sales": 300,
                    },
                ],
                requested_start=date(2021, 8, 2),
                requested_end=date(2026, 7, 16),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {"Code": "", "DiscDate": "2025-03-05", "Sales": 400},
                    {
                        "DiscNo": "synthetic-72060-2025-03-06",
                        "Code": "72060",
                        "DiscDate": "2025-03-06",
                        "Sales": 500,
                    },
                ],
                requested_start=date(2025, 3, 1),
                requested_end=date(2025, 3, 31),
            )
            conn = sqlite3.connect(db)
            try:
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, status FROM source_coverage "
                    "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_start"
                ).fetchall()
                early_covered = range_covered(
                    conn, "jquants_fin_summaries", date(2021, 8, 2), date(2025, 2, 28)
                )
                late_covered = range_covered(
                    conn, "jquants_fin_summaries", date(2025, 4, 1), date(2026, 7, 16)
                )
                across_covered = range_covered(
                    conn, "jquants_fin_summaries", date(2021, 8, 2), date(2026, 7, 16)
                )
            finally:
                conn.close()
            self.assertEqual(
                windows,
                [
                    ("2021-08-02", "2025-02-28", "ok"),
                    ("2025-03-01", "2025-03-31", "partial"),
                    ("2025-04-01", "2026-07-16", "ok"),
                ],
            )
            self.assertTrue(early_covered)
            self.assertTrue(late_covered)
            self.assertFalse(across_covered)

    def test_clean_refetch_clears_the_earlier_partial_complaint(self) -> None:
        """Re-fetching a range cleanly makes the stored rows good, so the previous
        quality complaint about that range no longer describes the store and is
        dropped instead of accumulating one row per failed chunk.

        The clean fetch covers a wider range than the failed one, so its coverage row
        is keyed differently: the complaint has to be deleted rather than overwritten.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [{"Code": "", "DiscDate": "2025-03-05", "Sales": 400}],
                requested_start=date(2025, 3, 1),
                requested_end=date(2025, 3, 31),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-72060-2025-03-05",
                        "Code": "72060",
                        "DiscDate": "2025-03-05",
                        "Sales": 500,
                    }
                ],
                requested_start=date(2025, 2, 1),
                requested_end=date(2025, 4, 30),
            )
            conn = sqlite3.connect(db)
            try:
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, status FROM source_coverage "
                    "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_start"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(windows, [("2025-02-01", "2025-04-30", "ok")])

    def test_a_narrower_failure_does_not_shrink_a_wider_complaint(self) -> None:
        """A complaint reaching past the fetched range still describes the months
        outside it, so a later failure inside it must not replace it. Narrowing would
        report one bad month where a bad quarter was found.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [{"Code": "", "DiscDate": "2025-02-05"}],
                requested_start=date(2025, 2, 1),
                requested_end=date(2025, 4, 30),
            )
            store_jquants_fin_summaries(
                db,
                [{"Code": "", "DiscDate": "2025-03-05"}],
                requested_start=date(2025, 3, 1),
                requested_end=date(2025, 3, 31),
            )
            conn = sqlite3.connect(db)
            try:
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, status FROM source_coverage "
                    "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_start"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                windows,
                [
                    ("2025-02-01", "2025-04-30", "partial"),
                    ("2025-03-01", "2025-03-31", "partial"),
                ],
            )

    def test_daily_bars_shifted_chunk_refetch_merges_coverage_window(self) -> None:
        """The shared coverage-merge path keeps daily_bars source_coverage contiguous
        so its record-count consistency check stays satisfied after a re-fetch with
        shifted chunk boundaries."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_daily_bars(
                db,
                [
                    {"Code": "72030", "Date": "2026-05-12", "C": 1000, "AdjC": 1000},
                    {"Code": "72030", "Date": "2026-05-27", "C": 1100, "AdjC": 1100},
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_daily_bars(
                db,
                [
                    {"Code": "72030", "Date": "2026-05-27", "C": 1100, "AdjC": 1100},
                    {"Code": "72030", "Date": "2026-06-02", "C": 1200, "AdjC": 1200},
                ],
                requested_start=date(2026, 5, 19),
                requested_end=date(2026, 6, 5),
            )
            conn = sqlite3.connect(db)
            try:
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, record_count FROM source_coverage "
                    "WHERE source = 'jquants_daily_bars' ORDER BY coverage_start"
                ).fetchall()
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(windows, [("2026-05-12", "2026-06-05", 3)])
            self.assertEqual(table_count, 3)

    def test_snapshot_and_single_day_stores_count_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"

            self.assertEqual(
                store_jquants_master(
                    db,
                    make_master_records(date(2026, 5, 8)),
                    requested_asof=date(2026, 5, 8),
                ),
                2500,
            )
            self.assertEqual(
                store_jpx_earnings_calendar_snapshot(db, _earnings_snapshot(date(2026, 5, 8))),
                1,
            )
            self.assertEqual(
                store_jquants_market_calendar(
                    db,
                    [
                        {"Date": "2026-05-08", "HolDiv": "1"},
                        {"Date": "2026-05-08", "HolDiv": "1"},
                    ],
                    requested_start=date(2026, 5, 8),
                    requested_end=date(2026, 5, 8),
                ),
                1,
            )
            self.assertEqual(
                store_edinet_documents(
                    db,
                    date(2026, 5, 8),
                    [
                        {
                            "seqNumber": 1,
                            "docID": "S100TEST",
                            "secCode": "72030",
                            "docTypeCode": "120",
                        },
                        {
                            "seqNumber": 2,
                            "docID": "S100TEST",
                            "secCode": "72030",
                            "docTypeCode": "120",
                        },
                    ],
                ),
                2,
            )
            self.assertEqual(
                store_edinet_metrics(
                    db,
                    date(2026, 5, 8),
                    [
                        {
                            "ticker": "7203",
                            "sales_ttm": 1,
                            "failure_reasons": [],
                            "extractor_revision": "a" * 64,
                        },
                        {
                            "ticker": "7203",
                            "sales_ttm": 2,
                            "failure_reasons": [],
                            "extractor_revision": "a" * 64,
                        },
                    ],
                ),
                1,
            )
            self.assertEqual(
                store_jpx_regulations(
                    db,
                    date(2026, 5, 8),
                    flags_by_ticker={"7203": ["特別注意銘柄", "特別注意銘柄"]},
                    source_names=["特別注意銘柄"],
                ),
                1,
            )

            conn = sqlite3.connect(db)
            try:
                coverage = dict(
                    conn.execute(
                        "SELECT source, record_count FROM source_coverage "
                        "WHERE source IN ("
                        "'jquants_master_snapshots', 'jpx_earnings_calendar', "
                        "'jquants_market_calendar', 'edinet_documents', 'edinet_metrics', "
                        "'jpx_regulation_flags')"
                    ).fetchall()
                )
            finally:
                conn.close()

            self.assertEqual(
                coverage,
                {
                    "jquants_master_snapshots": 2500,
                    "jpx_earnings_calendar": 1,
                    "jquants_market_calendar": 1,
                    "edinet_documents": 2,
                    "edinet_metrics": 1,
                    "jpx_regulation_flags": 1,
                },
            )

    def test_edinet_document_store_rejects_missing_or_duplicate_sequence_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            stored = {
                "seqNumber": 1,
                "docID": "S100KEPT",
                "secCode": "72030",
                "docTypeCode": "120",
            }
            store_edinet_documents(db, date(2026, 5, 8), [stored])

            with self.assertRaisesRegex(ValueError, "missing seqNumber"):
                store_edinet_documents(
                    db,
                    date(2026, 5, 8),
                    [{"docID": "S100MISSING"}],
                )
            with self.assertRaisesRegex(ValueError, "duplicate EDINET seqNumber"):
                store_edinet_documents(
                    db,
                    date(2026, 5, 8),
                    [
                        {"seqNumber": 2, "docID": "S100A"},
                        {"seqNumber": 2, "docID": "S100B"},
                    ],
                )
            for invalid_sequence in (True, 1.0, 1.5, " 1"):
                with (
                    self.subTest(invalid_sequence=invalid_sequence),
                    self.assertRaisesRegex(ValueError, "invalid EDINET seqNumber"),
                ):
                    store_edinet_documents(
                        db,
                        date(2026, 5, 8),
                        [{"seqNumber": invalid_sequence, "docID": "S100INVALID"}],
                    )

            conn = sqlite3.connect(db)
            try:
                rows = conn.execute(
                    "SELECT sequence_number, doc_id FROM edinet_documents "
                    "WHERE doc_date = '2026-05-08'"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [(1, "S100KEPT")])

    def test_edinet_metric_store_round_trips_extraction_and_document_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            asof = date(2026, 5, 8)
            revision = "a" * 64
            source_revision = "b" * 64
            store_edinet_metrics(
                db,
                asof,
                [
                    {
                        "ticker": "7203",
                        "sales_ttm": 100,
                        "investment_securities": 250,
                        "ocf_receivables_cash_effect": -10,
                        "ocf_inventories_cash_effect": 20,
                        "capex_ppe_reported": 30,
                        "extractor_revision": revision,
                        "source_document_revision": source_revision,
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "missing extractor_revision"):
                store_edinet_metrics(
                    db,
                    asof,
                    [{"ticker": "7203", "sales_ttm": 200}],
                )

            conn = sqlite3.connect(db)
            try:
                row = conn.execute(
                    "SELECT sales_ttm, investment_securities, ocf_receivables_cash_effect, "
                    "ocf_inventories_cash_effect, capex_ppe_reported, "
                    "ocf_contract_liabilities_cash_effect, extractor_revision, "
                    "source_document_revision "
                    "FROM edinet_metrics "
                    "WHERE asof_date = ? AND ticker = '7203'",
                    (asof.isoformat(),),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                row,
                (100.0, 250.0, -10.0, 20.0, 30.0, None, revision, source_revision),
            )

            for invalid in (-1, float("inf"), float("nan")):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(ValueError, "invalid investment_securities"),
                ):
                    store_edinet_metrics(
                        db,
                        asof,
                        [
                            {
                                "ticker": "7203",
                                "investment_securities": invalid,
                                "extractor_revision": revision,
                            }
                        ],
                    )


class DailyBarsCoverageTest(unittest.TestCase):
    def test_the_ten_day_imperial_transition_closure_is_not_a_missing_window(self) -> None:
        # The market was shut for the ten consecutive days of the 2019 imperial
        # transition, leaving eleven days between two trading days. Treating that as
        # a hole makes the bar store read as incomplete for every window covering it
        # and sends the fetch back for data it already holds.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                days = [
                    *(date(2019, 4, 22) + timedelta(days=offset) for offset in range(5)),
                    *(date(2019, 5, 7) + timedelta(days=offset) for offset in range(5)),
                ]
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [("7203", day.isoformat(), 1000.0) for day in days],
                )
                conn.commit()
                covered = daily_bars_covered_by_data(conn, date(2019, 4, 22), date(2019, 5, 11))
                # A whole fetch chunk missing still has to read as incomplete.
                conn.execute("DELETE FROM jquants_daily_bars WHERE traded_at > '2019-04-26'")
                conn.commit()
                after_deletion = daily_bars_covered_by_data(
                    conn, date(2019, 4, 22), date(2019, 6, 30)
                )
            finally:
                conn.close()
            self.assertTrue(covered)
            self.assertFalse(after_deletion)

    def test_a_fetch_chunk_holding_one_trading_day_is_not_covered(self) -> None:
        # The edge tolerances apply at both ends, so if they are wide enough to meet
        # in the middle of a 31-day fetch chunk, a chunk holding a single day reads as
        # covered and the rest is never fetched. Nothing downstream catches a hole
        # that small: the density check works in 120-day buckets.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                start, end = date(2024, 4, 1), date(2024, 5, 1)
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    ("7203", (start + timedelta(days=15)).isoformat(), 1000.0),
                )
                conn.commit()
                sparse = daily_bars_covered_by_data(conn, start, end)
                # The closure that forced the gap threshold up still has to be
                # reachable from a boundary that lands on its first day.
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [
                        ("7203", (date(2019, 5, 7) + timedelta(days=offset)).isoformat(), 1.0)
                        for offset in range(8)
                    ],
                )
                conn.commit()
                after_closure = daily_bars_covered_by_data(
                    conn, date(2019, 4, 27), date(2019, 5, 14)
                )
            finally:
                conn.close()
            self.assertFalse(sparse)
            self.assertTrue(after_closure)


class EmptyPayloadReplacementTest(unittest.TestCase):
    """A range fetch that returns nothing must not erase what the store holds."""

    @staticmethod
    def _bar(ticker: str, day: date) -> dict[str, object]:
        return {"Code": ticker, "Date": day.isoformat(), "C": 1000.0, "AdjC": 1000.0}

    def test_an_empty_bar_payload_refuses_to_replace_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_daily_bars(
                db,
                [self._bar("7203", date(2020, 12, 30))],
                requested_start=window[0],
                requested_end=window[1],
            )

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_daily_bars(db, [], requested_start=window[0], requested_end=window[1])

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
                # The refused replacement must preserve the previous coverage count.
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_daily_bars",),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(remaining, 1)
            self.assertEqual([row[0] for row in coverage], [1])

    def test_an_empty_payload_over_a_range_holding_nothing_is_accepted(self) -> None:
        # A genuinely empty stretch has to stay storable, or a window the market
        # never traded in would fail every pass.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            persisted = store_jquants_daily_bars(
                db, [], requested_start=date(2020, 1, 1), requested_end=date(2020, 1, 3)
            )
            self.assertEqual(persisted, 0)

    def test_an_empty_calendar_payload_refuses_to_replace_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_market_calendar(
                db,
                [{"Date": "2020-12-30", "HolDiv": "1"}],
                requested_start=window[0],
                requested_end=window[1],
            )

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_market_calendar(
                    db, [], requested_start=window[0], requested_end=window[1]
                )

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[
                    0
                ]
            finally:
                conn.close()
            self.assertEqual(remaining, 1)

    def test_a_non_empty_payload_still_replaces_what_the_range_held(self) -> None:
        # The guard must not cost the replace semantics that make a refetch
        # corrective: a name the provider no longer reports has to disappear.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_daily_bars(
                db,
                [self._bar("7203", date(2020, 12, 30)), self._bar("9999", date(2020, 12, 30))],
                requested_start=window[0],
                requested_end=window[1],
            )
            store_jquants_daily_bars(
                db,
                [self._bar("7203", date(2020, 12, 30))],
                requested_start=window[0],
                requested_end=window[1],
            )

            conn = open_connection(db)
            try:
                tickers = [
                    str(row[0])
                    for row in conn.execute("SELECT ticker FROM jquants_daily_bars ORDER BY ticker")
                ]
            finally:
                conn.close()
            self.assertEqual(tickers, ["7203"])

    def test_an_empty_fin_payload_refuses_to_replace_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "DiscNo": "synthetic-72030-2020-12-15",
                        "Code": "72030",
                        "DiscDate": "2020-12-15",
                        "EPS": "10",
                    }
                ],
                requested_start=window[0],
                requested_end=window[1],
            )

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_fin_summaries(
                    db, [], requested_start=window[0], requested_end=window[1]
                )

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(remaining, 1)


class WeeklyMarginStoreTest(unittest.TestCase):
    """Weekly margin balances, including the weeks the exchange does not publish."""

    @staticmethod
    def _record(code: str, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "Date": "2026-07-24",
            "Code": code,
            "LongVol": 5000.0,
            "ShrtVol": 1000.0,
            "LongStdVol": 3000.0,
            "LongNegVol": 2000.0,
            "ShrtStdVol": 800.0,
            "ShrtNegVol": 200.0,
            "IssType": "2",
        }
        record.update(overrides)
        return record

    def test_a_week_the_exchange_skipped_is_recorded_as_examined(self) -> None:
        # The endpoint answers with nothing for a week that has no balance date.
        # Without a coverage row the fetch would ask again on every pass, and the
        # reader could not tell "no balance date" from "nobody has looked".
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            persisted = store_jquants_weekly_margin(db, [], week_end=date(2017, 5, 5))

            self.assertEqual(persisted, 0)
            self.assertEqual(read_weekly_margin(db, date(2017, 5, 5)), [])
            self.assertIsNone(read_weekly_margin(db, date(2017, 5, 12)))

    def test_publication_candidates_wait_for_the_second_trading_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            trading_days = (
                date(2026, 7, 31),
                date(2026, 8, 3),
                date(2026, 8, 4),
                date(2026, 8, 5),
            )
            conn = open_connection(db)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [("7203", day.isoformat(), 1.0) for day in trading_days],
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                weekly_margin_candidate_dates(
                    db,
                    date(2026, 7, 31),
                    date(2026, 8, 3),
                    publication_asof=date(2026, 8, 3),
                ),
                [],
            )
            self.assertEqual(
                weekly_margin_candidate_dates(
                    db,
                    date(2026, 7, 31),
                    date(2026, 8, 4),
                    publication_asof=date(2026, 8, 4),
                ),
                [date(2026, 7, 31)],
            )

    def test_only_an_empty_snapshot_fetched_by_publication_day_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            week_end = date(2026, 7, 31)
            conn = open_connection(db)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [
                        ("7203", day.isoformat(), 1.0)
                        for day in (
                            week_end,
                            date(2026, 8, 3),
                            date(2026, 8, 4),
                            date(2026, 8, 5),
                        )
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            store_jquants_weekly_margin(db, [], week_end=week_end)
            conn = open_connection(db)
            try:
                conn.execute(
                    "UPDATE source_coverage SET fetched_at_utc = ? "
                    "WHERE source = 'jquants_weekly_margin'",
                    ("2026-08-04T07:00:00+00:00",),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertTrue(
                weekly_margin_empty_requires_refresh(db, week_end, asof=date(2026, 8, 5))
            )

            conn = open_connection(db)
            try:
                conn.execute(
                    "UPDATE source_coverage SET fetched_at_utc = ? "
                    "WHERE source = 'jquants_weekly_margin'",
                    ("2026-08-05T07:00:00+00:00",),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertFalse(
                weekly_margin_empty_requires_refresh(db, week_end, asof=date(2026, 8, 5))
            )

    def test_secondary_issue_lines_are_excluded_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            persisted = store_jquants_weekly_margin(
                db,
                [self._record("72030"), self._record("72035")],
                week_end=date(2026, 7, 24),
            )

            conn = open_connection(db)
            try:
                status = conn.execute(
                    "SELECT status, error FROM source_coverage WHERE source = ?",
                    ("jquants_weekly_margin",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(persisted, 1)
            self.assertEqual(status, ("ok", None))

    def test_a_zero_short_balance_is_stored_rather_than_dropped(self) -> None:
        # A 信用銘柄 has no stock lending, so zero is the instrument's normal
        # state. Treating it as missing would erase the distinction the axes need.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_weekly_margin(
                db,
                [self._record("13010", ShrtVol=0.0, ShrtStdVol=0.0, ShrtNegVol=0.0, IssType="1")],
                week_end=date(2026, 7, 24),
            )

            rows = read_weekly_margin(db, date(2026, 7, 24))
            self.assertIsNotNone(rows)
            assert rows is not None
            self.assertEqual(rows[0].short_vol, 0.0)
            self.assertEqual(rows[0].issue_type, "1")

    def test_an_empty_payload_refuses_to_replace_a_stored_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_weekly_margin(db, [self._record("72030")], week_end=date(2026, 7, 24))

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_weekly_margin(db, [], week_end=date(2026, 7, 24))

            rows = read_weekly_margin(db, date(2026, 7, 24))
            self.assertEqual(len(rows or []), 1)

    def test_weekly_store_rejects_post_transition_daily_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with self.assertRaisesRegex(ValueError, "legacy weekly series"):
                store_jquants_weekly_margin(
                    db,
                    [self._record("72030")],
                    week_end=date(2026, 9, 25),
                )
            self.assertFalse(db.exists())

    def test_nonempty_final_legacy_week_does_not_require_another_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self.assertTrue(final_legacy_week_requires_refresh(db))
            store_jquants_weekly_margin(
                db,
                [self._record("72030", Date="2026-09-18")],
                week_end=date(2026, 9, 18),
            )
            self.assertFalse(final_legacy_week_requires_refresh(db))

    def test_wrong_balance_date_cannot_replace_a_stored_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            day = date(2026, 7, 24)
            store_jquants_weekly_margin(db, [self._record("72030")], week_end=day)

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_weekly_margin(
                    db, [self._record("72030", Date="2026-07-17")], week_end=day
                )

            rows = read_weekly_margin(db, day)
            assert rows is not None
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].long_vol, 5000.0)


class MarginPublicationTransitionStoreTest(unittest.TestCase):
    @staticmethod
    def _all_issues_record(code: str, on_date: date) -> dict[str, object]:
        return {
            "Date": on_date.isoformat(),
            "Code": code,
            "LongVol": 5000.0,
            "ShrtVol": 1000.0,
            "LongStdVol": 3000.0,
            "LongNegVol": 2000.0,
            "ShrtStdVol": 800.0,
            "ShrtNegVol": 200.0,
            "IssType": "2",
        }

    def test_daily_all_issues_rejects_legacy_dates_but_accepts_identical_repetitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with self.assertRaisesRegex(ValueError, "daily all-issues series"):
                store_jquants_all_issues_daily_margin(
                    db,
                    [self._all_issues_record("72030", date(2026, 9, 18))],
                    balance_date=date(2026, 9, 18),
                )

            balance_date = date(2026, 9, 25)
            record = self._all_issues_record("72030", balance_date)
            persisted = store_jquants_all_issues_daily_margin(
                db, [record, record], balance_date=balance_date
            )
            conn = open_connection(db)
            try:
                row_count = conn.execute(
                    "SELECT COUNT(*) FROM jquants_all_issues_daily_margin"
                ).fetchone()[0]
                status = conn.execute(
                    "SELECT status FROM source_coverage "
                    "WHERE source = 'jquants_all_issues_daily_margin'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(persisted, 1)
            self.assertEqual(row_count, 1)
            self.assertEqual(status, "ok")
            self.assertEqual(len(read_all_issues_daily_margin(db, balance_date) or []), 1)

    def test_candidate_dates_have_no_transition_gap_and_skip_covered_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            trading_days = (date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29))
            store_jquants_daily_bars(
                db,
                [{"Date": day.isoformat(), "Code": "72030", "C": 100.0} for day in trading_days],
                requested_start=trading_days[0],
                requested_end=trading_days[-1],
            )

            self.assertEqual(
                all_issues_daily_margin_candidate_dates(db, date(2026, 9, 1), date(2026, 9, 28)),
                [date(2026, 9, 25)],
            )
            store_jquants_all_issues_daily_margin(
                db,
                [self._all_issues_record("72030", date(2026, 9, 25))],
                balance_date=date(2026, 9, 25),
            )
            self.assertEqual(
                all_issues_daily_margin_candidate_dates(db, date(2026, 9, 1), date(2026, 9, 29)),
                [date(2026, 9, 28)],
            )

    def test_payload_date_mismatch_cannot_replace_a_daily_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            balance_date = date(2026, 9, 25)
            store_jquants_all_issues_daily_margin(
                db,
                [self._all_issues_record("72030", balance_date)],
                balance_date=balance_date,
            )

            with self.assertRaises(EmptyRangeReplacementError):
                store_jquants_all_issues_daily_margin(
                    db,
                    [self._all_issues_record("72030", date(2026, 9, 28))],
                    balance_date=balance_date,
                )

            rows = read_all_issues_daily_margin(db, balance_date)
            self.assertEqual(len(rows or []), 1)

    def test_empty_first_publication_is_retried_instead_of_becoming_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_daily_bars(
                db,
                [
                    {"Date": "2026-09-25", "Code": "72030", "C": 100.0},
                    {"Date": "2026-09-28", "Code": "72030", "C": 101.0},
                ],
                requested_start=date(2026, 9, 25),
                requested_end=date(2026, 9, 28),
            )
            store_jquants_all_issues_daily_margin(db, [], balance_date=date(2026, 9, 25))

            self.assertIsNone(read_all_issues_daily_margin(db, date(2026, 9, 25)))
            self.assertEqual(
                all_issues_daily_margin_candidate_dates(db, date(2026, 9, 25), date(2026, 9, 28)),
                [date(2026, 9, 25)],
            )

    def test_missing_daily_balance_fields_are_partial_and_remain_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            balance_date = date(2026, 9, 25)
            persisted = store_jquants_all_issues_daily_margin(
                db,
                [{"Date": balance_date.isoformat(), "Code": "72030"}],
                balance_date=balance_date,
            )
            conn = open_connection(db)
            try:
                coverage = conn.execute(
                    "SELECT status, record_count FROM source_coverage "
                    "WHERE source = 'jquants_all_issues_daily_margin'"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(persisted, 0)
            self.assertEqual(coverage, ("partial", 0))
            self.assertIsNone(read_all_issues_daily_margin(db, balance_date))

    def test_margin_alerts_preserve_regulation_facts_in_a_separate_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            publication_date = date(2026, 8, 10)
            record = {
                "PubDate": publication_date.isoformat(),
                "Code": "72030",
                "AppDate": "2026-08-07",
                "PubReason": "日々公表",
                "ShrtOut": 1000,
                "ShrtOutChg": 10,
                "ShrtOutRatio": 0.2,
                "LongOut": 5000,
                "LongOutChg": 20,
                "LongOutRatio": 0.8,
                "SLRatio": 0.2,
                "ShrtNegOut": 200,
                "ShrtNegOutChg": 2,
                "ShrtStdOut": 800,
                "ShrtStdOutChg": 8,
                "LongNegOut": 2000,
                "LongNegOutChg": 10,
                "LongStdOut": 3000,
                "LongStdOutChg": 10,
                "TSEMrgnRegCls": "委託保証金率50%",
            }
            store_jquants_margin_alerts(
                db,
                [record],
                requested_start=publication_date,
                requested_end=publication_date,
            )

            alerts = read_margin_alerts(db, publication_date, publication_date)
            self.assertEqual(len(alerts or []), 1)
            assert alerts is not None
            self.assertEqual(alerts[0].applied_date, date(2026, 8, 7))
            self.assertEqual(
                alerts[0].tse_margin_regulation_classification,
                "委託保証金率50%",
            )
            conn = open_connection(db)
            try:
                weekly_count = conn.execute(
                    "SELECT COUNT(*) FROM jquants_weekly_margin"
                ).fetchone()[0]
                daily_count = conn.execute(
                    "SELECT COUNT(*) FROM jquants_all_issues_daily_margin"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual((weekly_count, daily_count), (0, 0))

    def test_margin_alert_missing_observation_fields_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            publication_date = date(2026, 8, 10)
            persisted = store_jquants_margin_alerts(
                db,
                [{"PubDate": publication_date.isoformat(), "Code": "72030"}],
                requested_start=publication_date,
                requested_end=publication_date,
            )
            conn = open_connection(db)
            try:
                coverage = conn.execute(
                    "SELECT status, record_count FROM source_coverage "
                    "WHERE source = 'jquants_margin_alerts'"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(persisted, 0)
            self.assertEqual(coverage, ("partial", 0))
            self.assertIsNone(read_margin_alerts(db, publication_date, publication_date))

    def test_backfill_window_includes_its_end_balance_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_daily_bars(
                db,
                [{"Date": "2026-09-25", "Code": "72030", "C": 100.0}],
                requested_start=date(2026, 9, 25),
                requested_end=date(2026, 9, 25),
            )

            self.assertEqual(
                all_issues_daily_margin_backfill_candidate_dates(
                    db, date(2026, 9, 25), date(2026, 9, 25)
                ),
                [date(2026, 9, 25)],
            )


class MarginPublicationLagTest(unittest.TestCase):
    @staticmethod
    def _seed(db: Path, week_ends: list[date], trading_days: list[date]) -> None:
        for week_end in week_ends:
            store_jquants_weekly_margin(
                db,
                [{"Date": week_end.isoformat(), "Code": "72030", "LongVol": 1.0}],
                week_end=week_end,
            )
        conn = open_connection(db)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_daily_bars"
                "(ticker, traded_at, close) VALUES (?, ?, ?)",
                [("7203", d.isoformat(), 1.0) for d in trading_days],
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_balance_date_is_not_readable_before_its_publication(self) -> None:
        # The exchange publishes a week's balances on the second trading day after
        # the balance date, in the late afternoon. Joining on the balance date, or
        # even on the publication day's close, would read positioning into a
        # decision that could not have seen it.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            week_end = date(2026, 7, 24)  # Friday
            trading = [date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29)]
            self._seed(db, [week_end], trading)

            self.assertEqual(published_margin_week_ends(db, date(2026, 7, 24)), [])
            self.assertEqual(published_margin_week_ends(db, date(2026, 7, 27)), [])
            # The exchange publishes late in the afternoon while a decision prices
            # at the close, so the publication day itself is still too early.
            self.assertEqual(published_margin_week_ends(db, date(2026, 7, 28)), [])
            self.assertEqual(published_margin_week_ends(db, date(2026, 7, 29)), [week_end])


class CoverageReadErrorsTest(unittest.TestCase):
    def test_unwritten_store_has_no_coverage(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            self.assertEqual(covered_intervals(connection, "jquants_fin_summaries"), ())
            self.assertFalse(
                daily_bars_covered_by_data(connection, date(2026, 5, 1), date(2026, 5, 31))
            )

    def test_invalid_coverage_schema_is_not_an_unfetched_range(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE source_coverage (source TEXT)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
                covered_intervals(connection, "jquants_fin_summaries")

    def test_invalid_bars_schema_is_not_an_unfetched_range(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE jquants_daily_bars (ticker TEXT)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
                daily_bars_covered_by_data(connection, date(2026, 5, 1), date(2026, 5, 31))


class RepeatedObservationTest(unittest.TestCase):
    """Repetition is harmless; conflicting values need a new authoritative fetch."""

    @staticmethod
    def _case(kind, db):
        day = date(2026, 7, 24) if kind == "weekly" else date(2026, 9, 25)
        record = {"Code": "72030", "Date": day.isoformat()}
        if kind == "bars":
            record["C"] = 1000
            store = partial(store_jquants_daily_bars, db, requested_start=day, requested_end=day)
            return store, record, "C", "jquants_daily_bars", "close", 1001
        if kind == "calendar":
            record = {"Date": day.isoformat(), "HolDiv": "1"}
            store = partial(
                store_jquants_market_calendar, db, requested_start=day, requested_end=day
            )
            return store, record, "HolDiv", "jquants_market_calendar", "holiday_division", "0"
        if kind == "alerts":
            record = {
                "PubDate": day.isoformat(),
                "Code": "72030",
                "AppDate": day.isoformat(),
                "PubReason": "日々公表",
                "TSEMrgnRegCls": "0",
                **dict.fromkeys(
                    (
                        "ShrtOut",
                        "ShrtOutChg",
                        "ShrtOutRatio",
                        "LongOut",
                        "LongOutChg",
                        "LongOutRatio",
                        "SLRatio",
                        "ShrtNegOut",
                        "ShrtNegOutChg",
                        "ShrtStdOut",
                        "ShrtStdOutChg",
                        "LongNegOut",
                        "LongNegOutChg",
                        "LongStdOut",
                        "LongStdOutChg",
                    ),
                    0,
                ),
            }
            store = partial(store_jquants_margin_alerts, db, requested_start=day, requested_end=day)
            return store, record, "LongOut", "jquants_margin_alerts", "long_outstanding", 10
        record.update(
            LongVol=5000,
            ShrtVol=1000,
            LongStdVol=3000,
            LongNegVol=2000,
            ShrtStdVol=800,
            ShrtNegVol=200,
            IssType="2",
        )
        if kind == "weekly":
            store = partial(store_jquants_weekly_margin, db, week_end=day)
            return store, record, "LongVol", "jquants_weekly_margin", "long_vol", 5100
        store = partial(store_jquants_all_issues_daily_margin, db, balance_date=day)
        return store, record, "LongVol", "jquants_all_issues_daily_margin", "long_vol", 5100

    def test_calendar_fetch_and_cached_projection_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Mock()
            client.get_mkt_calendar.return_value = [
                {"Date": "2026-09-27", "HolDiv": "2"},
                {"Date": "2026-09-25", "HolDiv": "1"},
                {"Date": "2026-09-25", "HolDiv": "1"},
                {"Date": "2026-09-26", "HolDiv": "0"},
            ]
            provider = JQuantsProvider(
                "token", client=client, sqlite_path=Path(tmp) / "market.sqlite"
            )
            first = provider.get_mkt_calendar(date(2026, 9, 25), date(2026, 9, 27))
            second = provider.get_mkt_calendar(date(2026, 9, 25), date(2026, 9, 27))
            self.assertEqual(first, second)
            self.assertEqual(
                [(row.day.day, row.is_business_day) for row in first],
                [(25, True), (26, False), (27, True)],
            )
            self.assertEqual(client.get_mkt_calendar.call_count, 1)

    def test_identical_records_conflicts_and_later_corrections(self):
        for kind in ("bars", "calendar", "weekly", "daily", "alerts"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "market.sqlite"
                store, record, field, table, column, correction = self._case(kind, db)
                self.assertEqual(store([record, record]), 1)
                with closing(sqlite3.connect(db)) as connection:
                    before = connection.execute(f"SELECT * FROM {table}").fetchall()
                    coverage = connection.execute("SELECT * FROM source_coverage").fetchall()
                    self.assertEqual(
                        connection.execute(f"SELECT {column} FROM {table}").fetchone(),
                        (record[field],),
                    )
                    self.assertEqual(
                        connection.execute("SELECT status FROM source_coverage").fetchone(), ("ok",)
                    )
                changed = dict(record, **{field: correction})
                for records in ([record, changed], [changed, record]):
                    with self.assertRaisesRegex(JQuantsProviderError, "conflicting"):
                        store(records)
                    with closing(sqlite3.connect(db)) as connection:
                        self.assertEqual(
                            connection.execute(f"SELECT * FROM {table}").fetchall(), before
                        )
                        self.assertEqual(
                            connection.execute("SELECT * FROM source_coverage").fetchall(), coverage
                        )
                self.assertEqual(store([changed]), 1)
                with closing(sqlite3.connect(db)) as connection:
                    self.assertEqual(
                        connection.execute(f"SELECT {column} FROM {table}").fetchone(),
                        (correction,),
                    )


if __name__ == "__main__":
    unittest.main()
