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

import pandas as pd
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.financial_documents import seed_legacy_financial_sqlite

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
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
    project_financial_documents,
    read_canonical_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.jquants_normalization import (
    normalize_financial_summaries,
)
from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQuantsProviderError,
    parse_jquants_code_parts,
)
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_margin_alerts,
    store_jquants_weekly_margin,
)
from baibai_engine.machine_analysis.observations.normalization import (
    code_quality,
    to_float,
    to_str_or_none,
)
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered


def _project_financial_records(records, asof):
    documents = [
        read_canonical_financial_summary(
            payload, ticker=ticker, disclosed_at=day, disclosure_number=identity
        )
        for ticker, day, identity, payload in normalize_financial_summaries(records).rows
    ]
    return project_financial_documents(documents, asof)


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
        self.assertIsNone(to_float(True))
        self.assertIsNone(to_float(False))
        self.assertIsNone(to_float(10**400))
        self.assertIsNone(to_float(-(10**400)))
        self.assertEqual(to_float(0), 0.0)
        self.assertEqual(to_str_or_none(0), "0")

    def test_fin_summaries_do_not_alias_average_shares_to_gross_issued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            seed_legacy_financial_sqlite(
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
            summary = normalize_financial_summary(raw)
            assert summary is not None
            self.assertIsNone(summary.shares_outstanding)
            self.assertEqual(summary.average_shares, 7_563_857.0)

    def test_fin_summaries_retain_both_forecast_slots_without_selecting(self) -> None:
        # Period selection belongs to L2; storage keeps both provider slots intact.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            seed_legacy_financial_sqlite(
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
        summaries = _project_financial_records(
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
            date(2026, 5, 13),
        )

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
        summaries = _project_financial_records(
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
            date(2026, 5, 13),
        )

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)

    def test_fin_summaries_keep_zero_current_forecasts(self) -> None:
        summaries = _project_financial_records(
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
            date(2026, 5, 13),
        )

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.forecast_eps, 0.0)
        self.assertEqual(summary.forecast_profit, 1_000_000_000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 900_000_000.0)
        self.assertEqual(summary.dps_forecast_annual, 0.0)

    def test_fin_summaries_forecast_profit_pair_are_selected_by_period(self) -> None:
        # Projection selects the applicable fiscal period from the retained slots.
        summaries = _project_financial_records(
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
            date(2026, 5, 13),
        )

        by_ticker = {summary.ticker: summary for summary in summaries}
        self.assertEqual(by_ticker["4849"].forecast_profit, 5464000000.0)
        self.assertEqual(by_ticker["4849"].forecast_ordinary_profit, 3406000000.0)
        self.assertEqual(by_ticker["4849"].fiscal_period, "1Q")
        self.assertEqual(by_ticker["4849"].fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(by_ticker["4849"].period_start, date(2026, 4, 1))
        self.assertEqual(by_ticker["4849"].period_end, date(2026, 6, 30))
        self.assertEqual(by_ticker["9715"].forecast_profit, 2000000000.0)
        self.assertEqual(by_ticker["9715"].forecast_ordinary_profit, 2500000000.0)

    def test_range_coverage_merges_shifted_complete_chunks(self) -> None:
        """A later bootstrap anchors chunk boundaries at a new asof, so a re-fetch only
        partially overlaps an existing window. Recording it must merge into the union,
        not delete-and-shrink the window and orphan its earlier part -- otherwise
        `range_covered` reports a gap even though the rows are still present.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            seed_legacy_financial_sqlite(
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
            seed_legacy_financial_sqlite(
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
            seed_legacy_financial_sqlite(
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
            seed_legacy_financial_sqlite(
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
            seed_legacy_financial_sqlite(
                db,
                [{"Code": "", "DiscDate": "2025-03-05", "Sales": 400}],
                requested_start=date(2025, 3, 1),
                requested_end=date(2025, 3, 31),
            )
            seed_legacy_financial_sqlite(
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
            seed_legacy_financial_sqlite(
                db,
                [{"Code": "", "DiscDate": "2025-02-05"}],
                requested_start=date(2025, 2, 1),
                requested_end=date(2025, 4, 30),
            )
            seed_legacy_financial_sqlite(
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

    def test_snapshot_and_single_day_stores_count_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"

            self.assertEqual(
                store_jpx_earnings_calendar_snapshot(db, _earnings_snapshot(date(2026, 5, 8))),
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
                        "'jpx_earnings_calendar', "
                        "'edinet_documents', 'edinet_metrics', "
                        "'jpx_regulation_flags')"
                    ).fetchall()
                )
            finally:
                conn.close()

            self.assertEqual(
                coverage,
                {
                    "jpx_earnings_calendar": 1,
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


class RepeatedObservationTest(unittest.TestCase):
    """Repetition is harmless; conflicting values need a new authoritative fetch."""

    @staticmethod
    def _case(kind, db):
        day = date(2026, 7, 24) if kind == "weekly" else date(2026, 9, 25)
        record = {"Code": "72030", "Date": day.isoformat()}
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

    def test_required_margin_booleans_are_rejected_and_literal_zero_is_usable(self):
        for kind in ("daily", "alerts"):
            for value in (False, True):
                with self.subTest(kind=kind, value=value), tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "market.sqlite"
                    store, record, field, table, column, _ = self._case(kind, db)
                    self.assertEqual(store([dict(record, **{field: value})]), 0)
                    with closing(sqlite3.connect(db)) as connection:
                        self.assertEqual(
                            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (0,)
                        )
                        self.assertEqual(
                            connection.execute("SELECT status FROM source_coverage").fetchone(),
                            ("partial",),
                        )
                    self.assertEqual(store([dict(record, **{field: 0})]), 1)
                    with closing(sqlite3.connect(db)) as connection:
                        self.assertEqual(
                            connection.execute(f"SELECT {column} FROM {table}").fetchone(), (0.0,)
                        )
                        self.assertEqual(
                            connection.execute("SELECT status FROM source_coverage").fetchone(),
                            ("ok",),
                        )

    def test_native_margin_codes_reject_nontext_without_excluding_valid_primary_rows(self):
        for kind in ("weekly", "daily", "alerts"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "market.sqlite"
                store, record, _, table, _, _ = self._case(kind, db)
                secondary = dict(record, Code="72031")
                self.assertEqual(store([record, secondary]), 1)
                with closing(sqlite3.connect(db)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT status FROM source_coverage").fetchone(),
                        ("ok",),
                    )
                self.assertEqual(
                    store(
                        [
                            record,
                            secondary,
                            *(dict(record, Code=code) for code in (True, False, 83060)),
                        ]
                    ),
                    1,
                )
                with closing(sqlite3.connect(db)) as connection:
                    self.assertEqual(
                        connection.execute(f"SELECT ticker FROM {table}").fetchall(), [("7203",)]
                    )
                    self.assertEqual(
                        connection.execute("SELECT status FROM source_coverage").fetchone(),
                        ("partial",),
                    )

    def test_identical_records_conflicts_and_later_corrections(self):
        for kind in ("weekly", "daily", "alerts"):
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
