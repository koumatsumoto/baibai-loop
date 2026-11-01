"""信用残の公表時差・系列切替と取得/読取の接続契約。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_daily_bars,
    store_jquants_margin_alerts,
    store_jquants_weekly_margin,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
    published_margin_week_ends,
    read_all_issues_daily_margin,
    read_margin_alerts,
    read_weekly_margin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.source_refresh.jquants import (
    all_issues_daily_margin_backfill_candidate_dates,
    all_issues_daily_margin_candidate_dates,
    final_legacy_week_requires_refresh,
    weekly_margin_candidate_dates,
    weekly_margin_empty_requires_refresh,
)


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

    def test_an_empty_payload_replaces_a_stored_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_weekly_margin(db, [self._record("72030")], week_end=date(2026, 7, 24))

            self.assertEqual(store_jquants_weekly_margin(db, [], week_end=date(2026, 7, 24)), 0)

            rows = read_weekly_margin(db, date(2026, 7, 24))
            self.assertEqual(rows, [])

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

            self.assertIsNone(read_weekly_margin(db, day))
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

            self.assertIsNone(read_all_issues_daily_margin(db, balance_date))
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
