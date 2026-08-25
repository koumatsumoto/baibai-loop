from __future__ import annotations

import copy
import io
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.market.jquants import JQuantsProviderError
from baibai_engine.screening.cli import bootstrap_cache_command
from baibai_engine.screening.cli.providers import ProviderBundle
from baibai_engine.screening.master_snapshot import validate_master_snapshot
from baibai_engine.screening.providers.jquants import JQuantsProvider
from baibai_engine.screening.sqlite_cache import open_connection, store_jquants_master
from baibai_engine.screening.sqlite_coverage.jquants import _append_master_snapshot_issues
from baibai_engine.screening.sqlite_reader import (
    read_eq_master_asof,
    read_eq_master_exact,
)


class _MasterClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_eq_master(self, *, date: str) -> list[dict[str, Any]]:
        self.calls.append(date)
        return make_master_records(__import__("datetime").date.fromisoformat(date))


class _BootstrapJQuants:
    def __init__(self, provider: JQuantsProvider, sqlite_path: Path) -> None:
        self.provider = provider
        self.sqlite_path = sqlite_path

    def get_eq_master(self, requested_asof: date):
        return self.provider.get_eq_master(requested_asof)

    def get_eq_bars_daily_range(self, start: date, end: date):
        del start, end
        return []

    def ensure_eq_bars_daily_range(self, start: date, end: date) -> int:
        del start, end
        return 0

    def get_adjustment_factor_bars_range(self, start: date, end: date):
        del start, end
        return []

    def get_fin_summary_range(self, start: date, end: date):
        del start, end
        return []

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        repair_ranges=(),
        progress=None,
    ) -> int:
        del start, revision_overlap_days, repair_ranges
        conn = open_connection(self.sqlite_path)
        tickers = [
            str(row[0])
            for row in conn.execute(
                "SELECT ticker FROM jquants_master_snapshots "
                "WHERE snapshot_date = ? AND is_common_stock = 1",
                (end.isoformat(),),
            )
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_fin_summaries("
            "ticker, disclosed_at, shares_outstanding, treasury_shares, "
            "equity_to_asset_ratio) VALUES (?, ?, ?, ?, ?)",
            [(ticker, end.isoformat(), 10_000_000.0, 1_000_000.0, 0.5) for ticker in tickers],
        )
        conn.commit()
        conn.close()
        if progress is not None:
            progress(1, 1, end, end)
        return len(tickers)

    def get_fy_summary_range(self, start: date, end: date):
        del start, end
        return []

    def get_mkt_calendar(self, start: date, end: date):
        del start, end
        return []

    def refresh_mkt_short_sale_report_range(self, start: date, end: date):
        del start, end
        return []

    def refresh_mkt_margin_alert_range(self, start: date, end: date):
        del start, end
        return []

    def get_mkt_all_issues_daily_margin(self, balance_date: date):
        del balance_date
        return []


class _BootstrapJPX:
    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        del asof_date
        return {}


def _db_fingerprint(db: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        rows = tuple(
            conn.execute(
                "SELECT snapshot_date, ticker, name, market, sector_33, is_common_stock "
                "FROM jquants_master_snapshots ORDER BY snapshot_date, ticker"
            ).fetchall()
        )
        coverage = tuple(
            conn.execute(
                "SELECT source, coverage_key, coverage_start, coverage_end, "
                "fetched_at_utc, record_count, status, error FROM source_coverage "
                "WHERE source = 'jquants_master_snapshots' ORDER BY coverage_key"
            ).fetchall()
        )
    finally:
        conn.close()
    return rows, coverage


class MasterSnapshotStoreTests(unittest.TestCase):
    def test_validation_logs_raw_persisted_and_intentional_exclusion_counts(self) -> None:
        asof = date(2026, 5, 29)
        with self.assertLogs("baibai_engine.screening.master_snapshot", level="INFO") as captured:
            validated = validate_master_snapshot(make_master_records(asof, excluded_count=2), asof)

        self.assertEqual(validated.raw_count, 2502)
        self.assertEqual(validated.persisted_count, 2500)
        self.assertEqual(validated.excluded_count, 2)
        self.assertIn("raw=2502 persisted=2500 intentional_excluded=2", captured.output[0])

    def test_two_dates_are_append_only_and_same_date_is_idempotent(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            first_records = make_master_records(first, excluded_count=2)
            self.assertEqual(
                store_jquants_master(db, first_records, requested_asof=first),
                2500,
            )
            first_rows, first_coverage = _snapshot_state(db, first)

            second_records = make_master_records(second)
            store_jquants_master(db, second_records, requested_asof=second)
            self.assertEqual(_snapshot_state(db, first), (first_rows, first_coverage))
            self.assertEqual(_snapshot_dates(db), [first.isoformat(), second.isoformat()])

            replacement = copy.deepcopy(second_records)
            replacement[0]["CoName"] = "Replacement"
            store_jquants_master(db, replacement, requested_asof=second)
            conn = sqlite3.connect(db)
            try:
                second_count = conn.execute(
                    "SELECT COUNT(*) FROM jquants_master_snapshots WHERE snapshot_date = ?",
                    (second.isoformat(),),
                ).fetchone()[0]
                replaced_name = conn.execute(
                    "SELECT name FROM jquants_master_snapshots "
                    "WHERE snapshot_date = ? AND ticker = '1000'",
                    (second.isoformat(),),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(second_count, 2500)
            self.assertEqual(replaced_name, "Replacement")
            self.assertEqual(_snapshot_state(db, first), (first_rows, first_coverage))

    def test_invalid_response_table_preserves_two_good_snapshots(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        requested = date(2026, 7, 31)
        base = make_master_records(requested)

        missing = copy.deepcopy(base)
        missing[0].pop("S33Nm")
        missing_date = copy.deepcopy(base)
        missing_date[0].pop("Date")
        unknown = copy.deepcopy(base)
        unknown[0]["Date"] = "unknown"
        mixed = copy.deepcopy(base)
        mixed[0]["Date"] = (requested + timedelta(days=1)).isoformat()
        mismatch = make_master_records(requested + timedelta(days=1))
        holiday_next_business_day = make_master_records(requested + timedelta(days=3))
        invalid_code = copy.deepcopy(base)
        invalid_code[0]["Code"] = "bad-code"
        duplicate = copy.deepcopy(base)
        duplicate[1]["Code"] = duplicate[0]["Code"]
        contradictory_type = copy.deepcopy(base)
        contradictory_type[0]["SecurityType"] = "preferred"

        invalid_cases = {
            "empty": [],
            "missing": missing,
            "missing_date": missing_date,
            "unknown_date": unknown,
            "mixed_date": mixed,
            "requested_mismatch": mismatch,
            "holiday_next_business_day": holiday_next_business_day,
            "invalid_code": invalid_code,
            "duplicate_ticker": duplicate,
            "small_population": make_master_records(requested, count=2499),
            "contradictory_security_type": contradictory_type,
        }

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(first), requested_asof=first)
            store_jquants_master(db, make_master_records(second), requested_asof=second)
            good_fingerprint = _db_fingerprint(db)

            for label, records in invalid_cases.items():
                with self.subTest(label=label):
                    with self.assertRaises(JQuantsProviderError):
                        store_jquants_master(db, records, requested_asof=requested)
                    self.assertEqual(_db_fingerprint(db), good_fingerprint)

    def test_invalid_response_does_not_create_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with self.assertRaises(JQuantsProviderError):
                store_jquants_master(db, [], requested_asof=date(2026, 5, 29))
            self.assertFalse(db.exists())

    def test_coverage_write_failure_rolls_back_same_date_replacement(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            before = _db_fingerprint(db)
            replacement = make_master_records(asof)
            replacement[0]["CoName"] = "must rollback"

            with (
                patch(
                    "baibai_engine.screening.sqlite_cache.jquants.record_source_coverage",
                    side_effect=sqlite3.OperationalError("injected coverage failure"),
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                store_jquants_master(db, replacement, requested_asof=asof)

            self.assertEqual(_db_fingerprint(db), before)

    def test_post_insert_count_mismatch_rolls_back_same_date_replacement(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            records = make_master_records(asof)
            store_jquants_master(db, records, requested_asof=asof)
            before = _db_fingerprint(db)
            validated = validate_master_snapshot(records, asof)
            inconsistent = SimpleNamespace(
                rows=validated.rows,
                persisted_count=validated.persisted_count + 1,
                raw_count=validated.raw_count,
                excluded_count=validated.excluded_count,
                common_stock_count=validated.common_stock_count,
            )

            with (
                patch(
                    "baibai_engine.screening.sqlite_cache.jquants.validate_master_snapshot",
                    return_value=inconsistent,
                ),
                self.assertRaisesRegex(JQuantsProviderError, "persisted count mismatch"),
            ):
                store_jquants_master(db, records, requested_asof=asof)

            self.assertEqual(_db_fingerprint(db), before)

    def test_insert_failure_rolls_back_same_date_replacement(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TRIGGER fail_master_insert BEFORE INSERT "
                "ON jquants_master_snapshots WHEN NEW.name = 'must rollback' "
                "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
            )
            conn.commit()
            conn.close()
            before = _db_fingerprint(db)
            replacement = make_master_records(asof)
            replacement[0]["CoName"] = "must rollback"

            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected insert failure"):
                store_jquants_master(db, replacement, requested_asof=asof)

            self.assertEqual(_db_fingerprint(db), before)

    def test_the_half_width_middle_dot_sector_is_stored_in_one_form(self) -> None:
        """J-Quants emits U+FF65 ("情報･通信業") and U+30FB ("情報・通信業") for the same
        TSE 33 sector. Two spellings of one sector split every sector-relative
        comparison downstream, so validation folds them before the row is persisted.
        """

        asof = date(2026, 5, 7)
        records = make_master_records(asof)
        records[0]["S33Nm"] = "情報･通信業"

        validated = validate_master_snapshot(records, asof)

        self.assertEqual(validated.rows[0][4], "情報・通信業")


class MasterSnapshotCoverageTests(unittest.TestCase):
    def test_non_integer_coverage_count_is_reported_as_an_issue(self) -> None:
        asof = date(2026, 5, 29)
        for invalid_count in ("not-an-integer", 2500.5):
            with (
                self.subTest(invalid_count=invalid_count),
                tempfile.TemporaryDirectory() as tmp,
            ):
                db = Path(tmp) / "market.sqlite"
                store_jquants_master(db, make_master_records(asof), requested_asof=asof)
                conn = open_connection(db)
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? "
                    "WHERE source = ? AND coverage_start = ?",
                    (invalid_count, "jquants_master_snapshots", asof.isoformat()),
                )
                conn.commit()
                issues = []
                _append_master_snapshot_issues(conn, issues, asof_date=asof)
                conn.close()

                self.assertTrue(any("not a non-negative integer" in i.reason for i in issues))

    def test_damage_is_scoped_to_requested_snapshot_date(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        for damaged in (first, second):
            for damage_kind in ("row", "coverage", "common_flag"):
                with (
                    self.subTest(damaged=damaged, damage_kind=damage_kind),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    db = Path(tmp) / "market.sqlite"
                    store_jquants_master(db, make_master_records(first), requested_asof=first)
                    store_jquants_master(db, make_master_records(second), requested_asof=second)
                    conn = open_connection(db)
                    if damage_kind == "row":
                        conn.execute(
                            "DELETE FROM jquants_master_snapshots "
                            "WHERE snapshot_date = ? AND ticker = '1000'",
                            (damaged.isoformat(),),
                        )
                    elif damage_kind == "coverage":
                        conn.execute(
                            "DELETE FROM source_coverage WHERE source = ? "
                            "AND coverage_start = ? AND coverage_end = ?",
                            (
                                "jquants_master_snapshots",
                                damaged.isoformat(),
                                damaged.isoformat(),
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE jquants_master_snapshots SET is_common_stock = 0 "
                            "WHERE snapshot_date = ? AND ticker = '1000'",
                            (damaged.isoformat(),),
                        )
                    conn.commit()

                    damaged_issues = []
                    _append_master_snapshot_issues(conn, damaged_issues, asof_date=damaged)
                    intact = second if damaged == first else first
                    intact_issues = []
                    _append_master_snapshot_issues(conn, intact_issues, asof_date=intact)
                    conn.close()

                    self.assertTrue(damaged_issues)
                    self.assertEqual(intact_issues, [])

    def test_bad_other_date_coverage_does_not_contaminate_requested_date(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(first), requested_asof=first)
            store_jquants_master(db, make_master_records(second), requested_asof=second)
            conn = open_connection(db)
            conn.execute(
                "UPDATE source_coverage SET status = 'failed', error = 'bad second' "
                "WHERE source = 'jquants_master_snapshots' AND coverage_start = ?",
                (second.isoformat(),),
            )
            conn.commit()
            issues = []
            _append_master_snapshot_issues(conn, issues, asof_date=first)
            conn.close()
            self.assertEqual(issues, [])


class MasterSnapshotReaderAndProviderTests(unittest.TestCase):
    def test_exact_prior_unavailable_and_latest_global_without_ghost_ticker(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        first_records = make_master_records(first)
        first_records.append(
            {
                "Date": first.isoformat(),
                "Code": "80000",
                "CoName": "Old only",
                "MktNm": "Prime",
                "S33Nm": "機械",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, first_records, requested_asof=first)
            store_jquants_master(db, make_master_records(second), requested_asof=second)

            self.assertEqual(len(read_eq_master_exact(db, first) or []), 2501)
            self.assertEqual(len(read_eq_master_exact(db, second) or []), 2500)
            prior = read_eq_master_asof(db, second - timedelta(days=1))
            self.assertEqual(prior.status, "prior_snapshot")
            self.assertEqual(prior.snapshot_date, first)
            fallback = read_eq_master_asof(db, first - timedelta(days=1))
            self.assertEqual(fallback.status, "future_snapshot")
            self.assertEqual(fallback.snapshot_date, first)

    def test_exact_reader_treats_corrupt_ticker_as_semantic_miss(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE jquants_master_snapshots SET ticker = '!' "
                "WHERE snapshot_date = ? AND ticker = '1000'",
                (asof.isoformat(),),
            )
            conn.commit()
            conn.close()
            self.assertIsNone(read_eq_master_exact(db, asof))

    def test_exact_reader_treats_non_integer_coverage_count_as_semantic_miss(self) -> None:
        asof = date(2026, 5, 29)
        for invalid_count in ("not-an-integer", 2500.5):
            with (
                self.subTest(invalid_count=invalid_count),
                tempfile.TemporaryDirectory() as tmp,
            ):
                db = Path(tmp) / "market.sqlite"
                store_jquants_master(db, make_master_records(asof), requested_asof=asof)
                conn = sqlite3.connect(db)
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? "
                    "WHERE source = ? AND coverage_start = ?",
                    (invalid_count, "jquants_master_snapshots", asof.isoformat()),
                )
                conn.commit()
                conn.close()

                self.assertIsNone(read_eq_master_exact(db, asof))

    def test_provider_prior_only_is_miss_and_cache_only_stops_with_date(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(first), requested_asof=first)

            no_fetch_client = _MasterClient()
            cache_only = JQuantsProvider(
                "token",
                Path(tmp) / "raw-cache-only",
                client=no_fetch_client,
                sqlite_path=db,
                cache_only=True,
            )
            with self.assertRaisesRegex(JQuantsProviderError, second.isoformat()):
                cache_only.get_eq_master(second)
            self.assertEqual(no_fetch_client.calls, [])

            fetch_client = _MasterClient()
            provider = JQuantsProvider(
                "token",
                Path(tmp) / "raw",
                client=fetch_client,
                sqlite_path=db,
            )
            self.assertEqual(len(provider.get_eq_master(second)), 2500)
            self.assertEqual(fetch_client.calls, [second.isoformat()])
            self.assertEqual(_snapshot_dates(db), [first.isoformat(), second.isoformat()])

    def test_provider_future_only_is_miss(self) -> None:
        requested = date(2026, 5, 29)
        future = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(future), requested_asof=future)
            client = _MasterClient()
            provider = JQuantsProvider("token", Path(tmp) / "raw", client=client, sqlite_path=db)

            self.assertEqual(len(provider.get_eq_master(requested)), 2500)
            self.assertEqual(client.calls, [requested.isoformat()])
            self.assertEqual(_snapshot_dates(db), [requested.isoformat(), future.isoformat()])

    def test_provider_refetches_when_exact_coverage_count_is_corrupt(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE source_coverage SET record_count = record_count + 1 "
                "WHERE source = ? AND coverage_start = ?",
                ("jquants_master_snapshots", asof.isoformat()),
            )
            conn.commit()
            conn.close()
            client = _MasterClient()
            provider = JQuantsProvider("token", Path(tmp) / "raw", client=client, sqlite_path=db)

            self.assertEqual(len(provider.get_eq_master(asof)), 2500)
            self.assertEqual(client.calls, [asof.isoformat()])
            self.assertIsNotNone(read_eq_master_exact(db, asof))

    def test_bootstrap_twice_forwards_asof_and_retains_both_snapshots(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            client = _MasterClient()
            provider = JQuantsProvider("token", Path(tmp) / "raw", client=client, sqlite_path=db)
            bundle = ProviderBundle(
                jquants=_BootstrapJQuants(provider, db),
                edinet=None,
                jpx=_BootstrapJPX(),
            )
            for asof in (first, second):
                self.assertEqual(
                    bootstrap_cache_command(
                        asof_date=asof,
                        providers=bundle,
                        sqlite_path=db,
                        stdout=io.StringIO(),
                    ),
                    0,
                )
            self.assertEqual(client.calls, [first.isoformat(), second.isoformat()])
            self.assertEqual(_snapshot_dates(db), [first.isoformat(), second.isoformat()])


def _snapshot_dates(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT snapshot_date FROM jquants_master_snapshots ORDER BY snapshot_date"
            ).fetchall()
        ]
    finally:
        conn.close()


def _snapshot_state(db: Path, asof: date) -> tuple[tuple[object, ...], tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        rows = tuple(
            conn.execute(
                "SELECT ticker, name, market, sector_33, is_common_stock "
                "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY ticker",
                (asof.isoformat(),),
            ).fetchall()
        )
        coverage = tuple(
            conn.execute(
                "SELECT coverage_key, coverage_start, coverage_end, fetched_at_utc, "
                "record_count, status, error FROM source_coverage "
                "WHERE source = 'jquants_master_snapshots' AND coverage_start = ? "
                "ORDER BY coverage_key",
                (asof.isoformat(),),
            ).fetchall()
        )
    finally:
        conn.close()
    return rows, coverage
