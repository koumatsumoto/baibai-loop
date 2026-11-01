"""原本投影の保存・空応答・重複とreaderの整合契約。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import requests
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.reader import read_fin_summaries
from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQuantsProviderError,
    parse_jquants_code_parts,
)
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_margin_alerts,
    store_jquants_market_calendar,
    store_jquants_master,
    store_jquants_weekly_margin,
)
from baibai_engine.machine_analysis.observations.normalization import (
    code_quality,
    to_float,
    to_str_or_none,
)
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh


def _earnings_snapshot(on_date: date = date(2026, 5, 15)) -> JPXEarningsCalendarSnapshot:
    return JPXEarningsCalendarSnapshot(
        entries=(JPXEarningsCalendarEntry(ticker="7203", announcement_date=on_date),),
        source_urls=("https://www.jpx.co.jp/kessan.xlsx",),
        raw_record_count=2,
        excluded_record_count=1,
        superseded_record_count=0,
    )


class SourceProjectionTests(unittest.TestCase):
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


class EmptyPayloadReplacementTest(unittest.TestCase):
    """A complete empty fetch replaces only its requested range."""

    @staticmethod
    def _bar(ticker: str, day: date) -> dict[str, object]:
        return {"Code": ticker, "Date": day.isoformat(), "C": 1000.0, "AdjC": 1000.0}

    def test_an_empty_bar_payload_replaces_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_daily_bars(
                db,
                [self._bar("7203", date(2020, 12, 30))],
                requested_start=window[0],
                requested_end=window[1],
            )

            self.assertEqual(
                store_jquants_daily_bars(
                    db, [], requested_start=window[0], requested_end=window[1]
                ),
                0,
            )

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_daily_bars",),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(remaining, 0)
            self.assertEqual([row[0] for row in coverage], [0])

    def test_an_empty_payload_over_a_range_holding_nothing_is_accepted(self) -> None:
        # A genuinely empty stretch has to stay storable, or a window the market
        # never traded in would fail every pass.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            persisted = store_jquants_daily_bars(
                db, [], requested_start=date(2020, 1, 1), requested_end=date(2020, 1, 3)
            )
            self.assertEqual(persisted, 0)

    def test_an_empty_calendar_payload_replaces_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_market_calendar(
                db,
                [{"Date": "2020-12-30", "HolDiv": "1"}],
                requested_start=window[0],
                requested_end=window[1],
            )

            self.assertEqual(
                store_jquants_market_calendar(
                    db, [], requested_start=window[0], requested_end=window[1]
                ),
                0,
            )

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[
                    0
                ]
            finally:
                conn.close()
            self.assertEqual(remaining, 0)

    def test_a_non_empty_payload_still_replaces_what_the_range_held(self) -> None:
        # A complete refetch replaces the scoped cross-section: a name the
        # provider no longer reports has to disappear.
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

    def test_an_empty_fin_payload_replaces_stored_rows(self) -> None:
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

            self.assertEqual(
                store_jquants_fin_summaries(
                    db, [], requested_start=window[0], requested_end=window[1]
                ),
                0,
            )

            conn = open_connection(db)
            try:
                remaining = conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(remaining, 0)


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
            session = Mock(spec=requests.Session)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {
                    "data": [
                        {"Date": "2026-09-27", "HolDiv": "2"},
                        {"Date": "2026-09-25", "HolDiv": "1"},
                        {"Date": "2026-09-25", "HolDiv": "1"},
                        {"Date": "2026-09-26", "HolDiv": "0"},
                    ]
                }
            ).encode()
            response._content_consumed = True
            session.get.return_value = response
            database = Path(tmp) / "market.sqlite"
            maintenance = JQuantsRefresh(
                database, None, client=JQuantsHTTP("token", session=session)
            )
            assert maintenance.ensure_market_calendar(date(2026, 9, 25), date(2026, 9, 27)) == 3
            first = JQuantsReader(database).get_mkt_calendar(date(2026, 9, 25), date(2026, 9, 27))
            assert maintenance.ensure_market_calendar(date(2026, 9, 25), date(2026, 9, 27)) == 3
            second = JQuantsReader(database).get_mkt_calendar(date(2026, 9, 25), date(2026, 9, 27))
            self.assertEqual(first, second)
            self.assertEqual(
                [(row.day.day, row.is_business_day) for row in first],
                [(25, True), (26, False), (27, True)],
            )
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(
                session.get.call_args.kwargs["params"],
                {"from": "20260925", "to": "20260927"},
            )

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
