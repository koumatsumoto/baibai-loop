"""系列別の信用残と対象範囲をSQLiteに保存する契約。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_margin_alerts,
    store_jquants_weekly_margin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


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
        # An examined week gets a zero-row coverage claim; an unexamined week does not.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            persisted = store_jquants_weekly_margin(db, [], week_end=date(2017, 5, 5))

            self.assertEqual(persisted, 0)
            with closing(open_connection(db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT coverage_start, status, record_count FROM source_coverage "
                        "WHERE source='jquants_weekly_margin'"
                    ).fetchall(),
                    [("2017-05-05", "ok", 0)],
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

            with closing(open_connection(db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT short_vol, issue_type FROM jquants_weekly_margin"
                    ).fetchall(),
                    [(0.0, "1")],
                )

    def test_an_empty_payload_replaces_a_stored_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_weekly_margin(db, [self._record("72030")], week_end=date(2026, 7, 24))

            self.assertEqual(store_jquants_weekly_margin(db, [], week_end=date(2026, 7, 24)), 0)

            with closing(open_connection(db)) as conn:
                self.assertEqual(
                    conn.execute("SELECT week_end FROM jquants_weekly_margin").fetchall(), []
                )

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

    def test_wrong_balance_date_marks_week_partial_instead_of_reusing_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            day = date(2026, 7, 24)
            store_jquants_weekly_margin(db, [self._record("72030")], week_end=day)

            self.assertEqual(
                store_jquants_weekly_margin(
                    db, [self._record("72030", Date="2026-07-17")], week_end=day
                ),
                0,
            )

            conn = open_connection(db)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT record_count, status FROM source_coverage "
                        "WHERE source = 'jquants_weekly_margin'"
                    ).fetchall(),
                    [(0, "partial")],
                )
            finally:
                conn.close()


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

    def test_payload_date_mismatch_marks_daily_snapshot_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            balance_date = date(2026, 9, 25)
            store_jquants_all_issues_daily_margin(
                db,
                [self._all_issues_record("72030", balance_date)],
                balance_date=balance_date,
            )

            self.assertEqual(
                store_jquants_all_issues_daily_margin(
                    db,
                    [self._all_issues_record("72030", date(2026, 9, 28))],
                    balance_date=balance_date,
                ),
                0,
            )

            conn = open_connection(db)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT record_count, status FROM source_coverage "
                        "WHERE source = 'jquants_all_issues_daily_margin'"
                    ).fetchall(),
                    [(0, "partial")],
                )
            finally:
                conn.close()

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

            conn = open_connection(db)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT publication_date, applied_date, tse_margin_regulation_classification "
                        "FROM jquants_margin_alerts"
                    ).fetchall(),
                    [(publication_date.isoformat(), "2026-08-07", "委託保証金率50%")],
                )
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
