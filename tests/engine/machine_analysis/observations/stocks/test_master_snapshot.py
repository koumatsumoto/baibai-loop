from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    master_from_payload,
    masters_from_payloads,
    validate_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection


class MasterSnapshotContractTests(unittest.TestCase):
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
        self.assertEqual(
            {code: json.loads(payload) for _, code, payload in validated.rows},
            {row["Code"]: row for row in records},
        )
        securities = masters_from_payloads([row[2] for row in validated.rows])
        self.assertEqual(len(securities), 2501)
        self.assertEqual(sum(security.is_common_stock for security in securities), 2500)
        self.assertFalse(
            next(security for security in securities if security.ticker == "9000").is_common_stock
        )

    def test_invalid_response_is_not_a_complete_requested_snapshot(self) -> None:
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
        boolean_code = copy.deepcopy(base)
        boolean_code[0]["Code"] = True
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
            "boolean_code": boolean_code,
            "duplicate_ticker": duplicate,
            "small_population": make_master_records(requested, count=2499),
            "missing_product_category": missing_product,
        }

        for label, records in invalid_cases.items():
            with self.subTest(label=label), self.assertRaises(JQuantsProviderError):
                validate_master_snapshot(records, requested)

    def test_sector_spelling_is_normalized_only_in_the_reading_projection(self) -> None:
        asof = date(2026, 5, 7)
        records = make_master_records(asof)
        records[0]["S33Nm"] = "情報･通信業"
        validated = validate_master_snapshot(records, asof)
        self.assertEqual(json.loads(validated.rows[0][2])["S33Nm"], "情報･通信業")
        self.assertEqual(masters_from_payloads([validated.rows[0][2]])[0].sector_33, "情報・通信業")


class MasterSnapshotReaderAndProviderTests(unittest.TestCase):
    def test_reader_does_not_refetch_to_hide_an_incompatible_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with closing(open_connection(db)) as conn, conn:
                conn.execute("PRAGMA user_version = 1")
            with (
                patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
                self.assertRaises(SQLiteSchemaError),
            ):
                read_master_snapshot(db, as_of=date(2026, 5, 29))

    def test_snapshot_selection_uses_exact_or_prior_but_never_future(self) -> None:
        first = date(2026, 5, 29)
        second = date(2026, 6, 30)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with closing(open_connection(db)) as conn, conn:
                insert_master_rows(
                    conn,
                    [
                        (first.isoformat(), "8000", "Old only", "Prime", "機械", "011"),
                        (first.isoformat(), "7203", "Previous", "Prime", "機械", "011"),
                        (second.isoformat(), "7203", "Current", "Prime", "機械", "011"),
                    ],
                )

            first_snapshot = read_master_snapshot(db, as_of=first)
            second_snapshot = read_master_snapshot(db, as_of=second)
            assert first_snapshot is not None
            assert second_snapshot is not None
            self.assertEqual(first_snapshot.as_of, first)
            self.assertEqual(second_snapshot.as_of, second)
            self.assertEqual(set(first_snapshot.securities), {"7203", "8000"})
            self.assertEqual(set(second_snapshot.securities), {"7203"})
            self.assertEqual(first_snapshot.securities["7203"].name, "Previous")
            self.assertEqual(second_snapshot.securities["7203"].name, "Current")
            self.assertEqual(
                read_master_snapshot(db, as_of=second - timedelta(days=1)), first_snapshot
            )
            self.assertIsNone(read_master_snapshot(db, as_of=first - timedelta(days=1)))

    def test_reader_rejects_corrupt_native_code(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            with closing(open_connection(db)) as conn, conn:
                insert_master_rows(
                    conn, [(asof.isoformat(), "1000", "Stored", "Prime", "機械", "011")]
                )
                conn.execute(
                    "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.Code','!')"
                )
            with self.assertRaisesRegex(JQuantsProviderError, "invalid J-Quants code"):
                read_master_snapshot(db, as_of=asof)


def test_indexed_master_never_stringifies_boolean_code_to_match_its_key():
    record = {
        "Code": True,
        "CoName": "合成企業",
        "MktNm": "Prime",
        "S33Nm": "機械",
        "ProdCat": "011",
    }
    with pytest.raises(JQuantsProviderError):
        master_from_payload(json.dumps(record), expected_code="TRUE")


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


def test_finite_master_read_uses_latest_market_date_and_only_requested_primary_issues(tmp_path):
    db = tmp_path / "market.sqlite"
    first, latest = date(2026, 5, 29), date(2026, 6, 30)
    with closing(open_connection(db)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (first.isoformat(), "5678", "旧市場のみ", "Prime", "サービス業", "011"),
                (latest.isoformat(), "12A4", "対象ETF", "ETF", "その他", "014"),
            ],
        )
        connection.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?, ?, ?)",
            [(latest.isoformat(), "56781", "{broken"), (latest.isoformat(), "99990", "{broken")],
        )
    selected = read_master_snapshot(db, as_of=latest, tickers=("12a4", "12A4", "5678"))
    assert selected is not None
    assert selected.as_of == latest
    assert list(selected.securities) == ["12A4"]
    assert selected.securities["12A4"].name == "対象ETF"
    assert not selected.securities["12A4"].is_common_stock
    assert read_master_snapshot(db, as_of=latest, tickers=("5678",)).securities == {}
    assert (
        read_master_snapshot(db, as_of=first, tickers=("5678",)).securities["5678"].name
        == "旧市場のみ"
    )
    with pytest.raises(JQuantsProviderError, match="valid JSON"):
        read_master_snapshot(db, as_of=latest, tickers=("9999",))
    with pytest.raises(JQuantsProviderError, match="valid JSON"):
        read_master_snapshot(db, as_of=latest)

    with closing(open_connection(db)) as connection, connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json, '$.Code','99990') "
            "WHERE security_code='12A40'"
        )
    with pytest.raises(JQuantsProviderError, match="differs from its key"):
        read_master_snapshot(db, as_of=latest, tickers=("12A4",))
