from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.sqlite.coverage import (
    EmptyRangeReplacementError,
    daily_bars_covered_by_data,
)
from baibai_engine.screening.providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
)
from baibai_engine.screening.sqlite_cache import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
    store_edinet_documents,
    store_edinet_metrics,
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_market_calendar,
    store_jquants_master,
)
from baibai_engine.screening.sqlite_reader import range_covered, read_fin_summaries
from tests.helpers.screening_sqlite import make_master_records


def _earnings_snapshot(on_date: date = date(2026, 5, 15)) -> JPXEarningsCalendarSnapshot:
    return JPXEarningsCalendarSnapshot(
        entries=(JPXEarningsCalendarEntry(ticker="7203", announcement_date=on_date),),
        source_urls=("https://www.jpx.co.jp/kessan.xlsx",),
        raw_record_count=2,
        excluded_record_count=1,
        rejected_record_count=0,
    )


class SQLiteCacheTest(unittest.TestCase):
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

            with self.assertRaisesRegex(SQLiteSchemaError, "unsupported screening SQLite schema"):
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
                        "Close": 1000,
                        "AdjustmentClose": 1000,
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
                        "Close": 1000,
                        "AdjustmentClose": 1000,
                    },
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "Close": 1001,
                        "AdjustmentClose": 1001,
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
                    {"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 100},
                    {"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 101},
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

    def test_fin_summaries_forecast_eps_uses_short_keys_with_next_year_fallback(self) -> None:
        # ClientV2 fin-summary payloads use short keys: FEPS (current-FY forecast) is
        # empty on full-year disclosures, where guidance moves to NxFEPS. The stored
        # forecast_eps (the per_forward basis) must take NxFEPS on FY rows and keep
        # FEPS on interim rows.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "Code": "97150",
                        "DisclosedDate": "2026-04-30",
                        "FEPS": "",
                        "NxFEPS": "360.26",
                    },
                    {
                        "Code": "72030",
                        "DisclosedDate": "2026-05-13",
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
                        "SELECT ticker, forecast_eps FROM jquants_fin_summaries"
                    ).fetchall()
                )
            finally:
                conn.close()
            self.assertEqual(stored["9715"], 360.26)
            self.assertEqual(stored["7203"], 306.89)

    def test_fin_summaries_forecast_profit_pair_round_trips_by_period(self) -> None:
        # 予想純利益/経常は forecast_eps と同一予想期から採って保存・読戻す。当期予想
        # EPS(FEPS)がある四半期開示は当期ペア(FNP/FOdP)、FEPS 空の本決算開示は翌期
        # ペア(NxFNp/NxFOdP)を採る。純利益>経常の行が一時益 flag の一次入力になる。
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {
                        "Code": "48490",
                        "DisclosedDate": "2026-05-13",
                        "FEPS": "138.72",
                        "FNP": "5464000000",
                        "FOdP": "3406000000",
                        "NxFNp": "900000000",
                        "NxFOdP": "1200000000",
                    },
                    {
                        "Code": "97150",
                        "DisclosedDate": "2026-04-30",
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
                    {"Code": "72030", "DisclosedDate": "2026-05-13", "NetSales": 100},
                    {"Code": "72040", "DisclosedDate": "2026-05-27", "NetSales": 200},
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {"Code": "72040", "DisclosedDate": "2026-05-27", "NetSales": 200},
                    {"Code": "72050", "DisclosedDate": "2026-06-02", "NetSales": 300},
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
                    {"Code": "72030", "DisclosedDate": "2021-08-02", "NetSales": 100},
                    {"Code": "72040", "DisclosedDate": "2024-01-10", "NetSales": 200},
                    {"Code": "72050", "DisclosedDate": "2026-07-16", "NetSales": 300},
                ],
                requested_start=date(2021, 8, 2),
                requested_end=date(2026, 7, 16),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {"Code": "", "DisclosedDate": "2025-03-05", "NetSales": 400},
                    {"Code": "72060", "DisclosedDate": "2025-03-06", "NetSales": 500},
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
                [{"Code": "", "DisclosedDate": "2025-03-05", "NetSales": 400}],
                requested_start=date(2025, 3, 1),
                requested_end=date(2025, 3, 31),
            )
            store_jquants_fin_summaries(
                db,
                [{"Code": "72060", "DisclosedDate": "2025-03-05", "NetSales": 500}],
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
                [{"Code": "", "DisclosedDate": "2025-02-05"}],
                requested_start=date(2025, 2, 1),
                requested_end=date(2025, 4, 30),
            )
            store_jquants_fin_summaries(
                db,
                [{"Code": "", "DisclosedDate": "2025-03-05"}],
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
                    {"Code": "72030", "Date": "2026-05-12", "Close": 1000, "AdjustmentClose": 1000},
                    {"Code": "72030", "Date": "2026-05-27", "Close": 1100, "AdjustmentClose": 1100},
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_daily_bars(
                db,
                [
                    {"Code": "72030", "Date": "2026-05-27", "Close": 1100, "AdjustmentClose": 1100},
                    {"Code": "72030", "Date": "2026-06-02", "Close": 1200, "AdjustmentClose": 1200},
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
                        {"Date": "2026-05-08", "HolidayDivision": "1"},
                        {"Date": "2026-05-08", "HolidayDivision": "0"},
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
                    "SELECT sales_ttm, extractor_revision, source_document_revision "
                    "FROM edinet_metrics "
                    "WHERE asof_date = ? AND ticker = '7203'",
                    (asof.isoformat(),),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, (100.0, revision, source_revision))

    def test_direct_stores_do_not_persist_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [{"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 100}],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            store_jquants_master(
                db,
                make_master_records(date(2026, 5, 8)),
                requested_asof=date(2026, 5, 8),
            )
            store_jpx_earnings_calendar_snapshot(db, _earnings_snapshot())
            store_jquants_market_calendar(
                db,
                [{"Date": "2026-05-08", "HolidayDivision": "1"}],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            store_edinet_documents(
                db,
                date(2026, 5, 8),
                [
                    {
                        "seqNumber": 1,
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "submitDateTime": "2026-05-08 15:00",
                    }
                ],
            )
            store_edinet_metrics(
                db,
                date(2026, 5, 8),
                [
                    {
                        "ticker": "7203",
                        "sales_ttm": 1,
                        "failure_reasons": [],
                        "extractor_revision": "a" * 64,
                    }
                ],
            )
            store_jpx_regulations(
                db,
                date(2026, 5, 8),
                flags_by_ticker={"7203": ["特別注意銘柄"]},
                source_names=["特別注意銘柄"],
            )

            conn = sqlite3.connect(db)
            try:
                for table in (
                    "jquants_fin_summaries",
                    "jquants_master_snapshots",
                    "jquants_earnings_calendar",
                    "jquants_market_calendar",
                    "edinet_documents",
                ):
                    columns = [
                        str(row[1])
                        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    ]
                    self.assertNotIn("raw_json", columns)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()


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
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close) "
                    "VALUES (?, ?, ?, ?)",
                    [("7203", day.isoformat(), 1000.0, 1000.0) for day in days],
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
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close) "
                    "VALUES (?, ?, ?, ?)",
                    ("7203", (start + timedelta(days=15)).isoformat(), 1000.0, 1000.0),
                )
                conn.commit()
                sparse = daily_bars_covered_by_data(conn, start, end)
                # The closure that forced the gap threshold up still has to be
                # reachable from a boundary that lands on its first day.
                conn.executemany(
                    "INSERT INTO jquants_daily_bars"
                    "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
                    [
                        ("7203", (date(2019, 5, 7) + timedelta(days=offset)).isoformat(), 1.0, 1.0)
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
        return {"Code": ticker, "Date": day.isoformat(), "Close": 1000.0, "AdjustmentClose": 1000.0}

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
                # The refusal must leave no coverage row claiming the range is fine.
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

    def test_an_empty_fin_payload_refuses_to_replace_stored_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            window = (date(2020, 12, 1), date(2020, 12, 31))
            store_jquants_fin_summaries(
                db,
                [{"Code": "72030", "DiscDate": "2020-12-15", "EPS": "10"}],
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
