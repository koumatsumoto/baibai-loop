from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_master
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    masters_from_payloads,
    validate_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_asof,
    read_eq_master_exact,
    read_master_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection
from baibai_jobs.maintenance.jquants_inputs import (
    JQuantsReader,
)


def _db_fingerprint(db: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        rows = tuple(
            conn.execute(
                "SELECT snapshot_date, security_code, record_json "
                "FROM jquants_master_snapshots ORDER BY snapshot_date, security_code"
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
    def test_native_snapshot_preserves_secondary_classes_and_all_provider_fields(self) -> None:
        asof = date(2026, 5, 29)
        records = make_master_records(asof, secondary_class_count=2)
        records[0].update(CoNameEn="Source name", S17="1", ScaleCat="TOPIX Small", Mrgn="2")
        records.append(dict(records[0], Code="90000", CoName="ETF native record", ProdCat="014"))
        validated = validate_master_snapshot(records, asof)
        self.assertEqual(validated.persisted_count, 2503)
        self.assertEqual(json.loads(validated.rows[0][2]), records[0])
        self.assertEqual(json.loads(validated.rows[-1][2]), records[-1])
        self.assertTrue(validated.rows[-2][1].endswith("1"))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self.assertEqual(store_jquants_master(db, records, requested_asof=asof), 2503)
            with closing(sqlite3.connect(db)) as conn:
                persisted = conn.execute(
                    "SELECT security_code, record_json FROM jquants_master_snapshots "
                    "ORDER BY security_code"
                ).fetchall()
            self.assertEqual(
                {code: json.loads(payload) for code, payload in persisted},
                {row["Code"]: row for row in records},
            )
            projected = read_eq_master_exact(db, asof)
            assert projected is not None
            self.assertEqual(len(projected), 2501)
            self.assertEqual(sum(row.is_common_stock for row in projected), 2500)
            self.assertFalse(next(row for row in projected if row.ticker == "9000").is_common_stock)

    def test_two_dates_are_append_only_and_same_date_is_idempotent(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            first_records = make_master_records(first, secondary_class_count=2)
            self.assertEqual(
                store_jquants_master(db, first_records, requested_asof=first),
                2502,
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
                    "SELECT json_extract(record_json, '$.CoName') FROM jquants_master_snapshots "
                    "WHERE snapshot_date = ? AND security_code = '10000'",
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
        missing_product = copy.deepcopy(base)
        missing_product[0].pop("ProdCat")

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
            "missing_product_category": missing_product,
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
                    "baibai_engine.machine_analysis.observations.jquants_writer.record_source_coverage",
                    side_effect=sqlite3.OperationalError("injected coverage failure"),
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                store_jquants_master(db, replacement, requested_asof=asof)

            self.assertEqual(_db_fingerprint(db), before)

    def test_insert_failure_rolls_back_same_date_replacement(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "CREATE TRIGGER fail_master_insert BEFORE INSERT "
                    "ON jquants_master_snapshots WHEN json_extract(NEW.record_json, '$.CoName') = 'must rollback' "
                    "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
                )
                conn.commit()
            before = _db_fingerprint(db)
            replacement = make_master_records(asof)
            replacement[0]["CoName"] = "must rollback"

            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected insert failure"):
                store_jquants_master(db, replacement, requested_asof=asof)

            self.assertEqual(_db_fingerprint(db), before)

    def test_sector_spelling_is_normalized_only_in_the_reading_projection(self) -> None:
        asof = date(2026, 5, 7)
        records = make_master_records(asof)
        records[0]["S33Nm"] = "情報･通信業"
        validated = validate_master_snapshot(records, asof)
        self.assertEqual(json.loads(validated.rows[0][2])["S33Nm"], "情報･通信業")
        self.assertEqual(masters_from_payloads([validated.rows[0][2]])[0].sector_33, "情報・通信業")


class MasterSnapshotReaderAndProviderTests(unittest.TestCase):
    def test_provider_does_not_refetch_to_hide_an_incompatible_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with closing(open_connection(db)) as conn, conn:
                conn.execute("PRAGMA user_version = 1")
            with (
                patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
                self.assertRaises(SQLiteSchemaError),
            ):
                JQuantsReader(db).get_eq_master(date(2026, 5, 29))

    def test_exact_prior_and_future_snapshot_selection(self) -> None:
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
                "ProdCat": "011",
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
            indexed = read_master_snapshot(db, as_of=second - timedelta(days=1))
            assert indexed is not None
            self.assertEqual(indexed.as_of, first)
            self.assertEqual(
                indexed.securities, {security.ticker: security for security in prior.masters}
            )
            fallback = read_eq_master_asof(db, first - timedelta(days=1))
            self.assertEqual(fallback.status, "future_snapshot")
            self.assertEqual(fallback.snapshot_date, first)

    def test_exact_reader_rejects_corrupt_native_code(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_master(db, make_master_records(asof), requested_asof=asof)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "UPDATE jquants_master_snapshots SET record_json = json_set(record_json, '$.Code', '!') "
                    "WHERE snapshot_date = ? AND security_code = '10000'",
                    (asof.isoformat(),),
                )
                conn.commit()
            with self.assertRaises(JQuantsProviderError):
                read_eq_master_exact(db, asof)

    def test_exact_reader_treats_non_integer_coverage_count_as_semantic_miss(self) -> None:
        asof = date(2026, 5, 29)
        for invalid_count in ("not-an-integer", 2500.5):
            with (
                self.subTest(invalid_count=invalid_count),
                tempfile.TemporaryDirectory() as tmp,
            ):
                db = Path(tmp) / "market.sqlite"
                store_jquants_master(db, make_master_records(asof), requested_asof=asof)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "UPDATE source_coverage SET record_count = ? "
                        "WHERE source = ? AND coverage_start = ?",
                        (invalid_count, "jquants_master_snapshots", asof.isoformat()),
                    )
                    conn.commit()

                self.assertIsNone(read_eq_master_exact(db, asof))


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
                "SELECT security_code, record_json "
                "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY security_code",
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


def test_malformed_stored_master_is_a_source_error_not_an_empty_snapshot():
    from baibai_jobs.observability.tradingview import (
        failure_category,
    )

    with pytest.raises(JQuantsProviderError) as caught:
        masters_from_payloads(["{broken"])
    assert failure_category(caught.value) == "storage"
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize("field", ["CoName", "MktNm", "S33Nm"])
@pytest.mark.parametrize("missing", [False, True])
def test_invalid_stored_master_projection_is_a_source_error(field, missing):
    record = {
        "Date": "2026-07-20",
        "Code": "23310",
        "CoName": "保有企業",
        "MktNm": "Prime",
        "S33Nm": "サービス業",
        "ProdCat": "011",
    }
    if missing:
        del record[field]
    else:
        record[field] = " "
    with pytest.raises(JQuantsProviderError, match="invalid security fields") as caught:
        masters_from_payloads([json.dumps(record)])
    assert isinstance(caught.value.__cause__, ValidationError)
