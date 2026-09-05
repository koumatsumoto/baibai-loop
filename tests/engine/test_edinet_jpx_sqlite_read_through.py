from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.screening.providers.edinet import (
    EDINETProvider,
    EDINETProviderError,
    select_document_candidates,
)
from baibai_engine.screening.providers.jpx import JPXProvider, JPXProviderError
from baibai_engine.screening.sqlite_cache import (
    open_connection,
    store_edinet_documents,
    store_jpx_regulations,
)
from baibai_engine.screening.sqlite_reader import (
    has_jpx_regulation_data,
    read_edinet_documents,
    read_edinet_metrics,
    read_jpx_regulations,
)


def _add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    date_iso: str,
) -> None:
    add_source_coverage(
        conn,
        source=source,
        coverage_key=date_iso,
        record_count=1,
        min_date=date_iso,
        max_date=date_iso,
    )


class EDINETSQLiteReaderTests(unittest.TestCase):
    def test_read_edinet_documents_returns_none_when_date_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            self.assertIsNone(read_edinet_documents(sqlite_path, date(2026, 4, 24)))

    def test_read_edinet_documents_returns_records_for_imported_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code"
                ") VALUES (?, ?, ?, ?, ?)",
                ("2026-04-24", 1, "S100ABCD", "13010", "120"),
            )
            _add_source_coverage(conn, source="edinet_documents", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            docs = read_edinet_documents(sqlite_path, date(2026, 4, 24))
            assert docs is not None
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["docID"], "S100ABCD")

    def test_read_edinet_documents_preserves_description_period_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            store_edinet_documents(
                sqlite_path,
                date(2026, 4, 24),
                [
                    {
                        "seqNumber": 1,
                        "docID": "S100ABCD",
                        "secCode": "13010",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "docDescription": "有価証券報告書 2025/04/01-2026/03/31",
                    }
                ],
            )

            docs = read_edinet_documents(sqlite_path, date(2026, 4, 24))
            assert docs is not None
            self.assertEqual(docs[0]["docDescription"], "有価証券報告書 2025/04/01-2026/03/31")
            candidates = select_document_candidates(docs).candidates

            self.assertEqual(candidates["1301"].period_start, date(2025, 4, 1))
            self.assertEqual(candidates["1301"].period_end, date(2026, 3, 31))

    def test_sparse_edit_event_round_trip_preserves_origin_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            store_edinet_documents(
                sqlite_path,
                date(2026, 4, 24),
                [
                    {
                        "seqNumber": 1,
                        "docID": "S100ABCD",
                        "secCode": "13010",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "submitDateTime": "2026-04-24 09:00",
                    }
                ],
            )
            store_edinet_documents(
                sqlite_path,
                date(2026, 4, 25),
                [
                    {
                        "seqNumber": 2,
                        "docID": "S100ABCD",
                        "docInfoEditStatus": "1",
                        "opeDateTime": "2026-04-25 09:00",
                    }
                ],
            )

            origin_rows = read_edinet_documents(sqlite_path, date(2026, 4, 24))
            event_rows = read_edinet_documents(sqlite_path, date(2026, 4, 25))
            assert origin_rows is not None
            assert event_rows is not None
            original = select_document_candidates(origin_rows).candidates["1301"]
            edited = select_document_candidates([*origin_rows, *event_rows]).candidates["1301"]

            self.assertEqual(edited.doc_id, "S100ABCD")
            self.assertEqual(edited.doc_type_code, "120")
            self.assertNotEqual(
                edited.source_document_revision,
                original.source_document_revision,
            )

    def test_read_edinet_metrics_returns_keyed_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO edinet_metrics("
                "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
                "source_submit_datetime, source_period_start, source_period_end, "
                "investment_securities"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-04-24",
                    "1301",
                    1_000_000.0,
                    200_000.0,
                    50_000.0,
                    80_000.0,
                    300_000.0,
                    "consolidated",
                    "exact",
                    "exact",
                    "approximated",
                    "2026-04-01 12:00",
                    "2025-04-01",
                    "2026-03-31",
                    250_000.0,
                ),
            )
            _add_source_coverage(conn, source="edinet_metrics", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            records = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert records is not None
            self.assertIn("1301", records)
            self.assertEqual(records["1301"].sales_ttm, 1_000_000.0)
            self.assertEqual(records["1301"].consolidation_basis, "consolidated")
            self.assertEqual(records["1301"].source_submit_datetime, "2026-04-01 12:00")
            self.assertEqual(records["1301"].source_period_start, date(2025, 4, 1))
            self.assertEqual(records["1301"].source_period_end, date(2026, 3, 31))
            self.assertEqual(records["1301"].investment_securities, 250_000.0)


class JPXSQLiteReaderTests(unittest.TestCase):
    def test_read_jpx_regulations_returns_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.executemany(
                "INSERT INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                [
                    ("2026-04-24", "特別注意銘柄", "1301", "特別注意銘柄", None),
                    ("2026-04-24", "整理銘柄", "1301", "整理銘柄", None),
                    ("2026-04-24", "取引停止", "1302", "取引停止", None),
                ],
            )
            _add_source_coverage(conn, source="jpx_regulation_flags", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            snapshot = read_jpx_regulations(sqlite_path, date(2026, 4, 24))
            assert snapshot is not None
            self.assertEqual(snapshot.flags_by_ticker["1301"], ("整理銘柄", "特別注意銘柄"))
            self.assertEqual(snapshot.flags_by_ticker["1302"], ("取引停止",))

    def test_has_jpx_regulation_data_false_when_no_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            self.assertFalse(has_jpx_regulation_data(sqlite_path, date(2026, 4, 24)))

    def test_has_jpx_regulation_data_true_for_imported_empty_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _add_source_coverage(conn, source="jpx_regulation_flags", date_iso="2026-04-24")
            conn.commit()
            conn.close()
            self.assertTrue(has_jpx_regulation_data(sqlite_path, date(2026, 4, 24)))


class EDINETProviderReadThroughTests(unittest.TestCase):
    def test_list_documents_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code"
                ") VALUES (?, ?, ?, ?, ?)",
                ("2026-04-24", 1, "S100A", "13010", "120"),
            )
            _add_source_coverage(conn, source="edinet_documents", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            provider = EDINETProvider("api-key", cache_dir, sqlite_path=sqlite_path)

            docs = provider.list_documents(date(2026, 4, 24))

            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["docID"], "S100A")

    def test_load_metric_records_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO edinet_metrics("
                "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
                "source_submit_datetime, source_period_start, source_period_end"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-04-24",
                    "1301",
                    1_000_000.0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "exact",
                    "exact",
                    "approximated",
                    "2026-04-01 12:00",
                    "2025-04-01",
                    "2026-03-31",
                ),
            )
            _add_source_coverage(conn, source="edinet_metrics", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            provider = EDINETProvider("api-key", cache_dir, sqlite_path=sqlite_path)

            records = provider.load_metric_records(date(2026, 4, 24))

            self.assertIn("1301", records)
            self.assertEqual(records["1301"].sales_ttm, 1_000_000.0)
            self.assertEqual(records["1301"].source_submit_datetime, "2026-04-01 12:00")

    def test_cache_only_load_metric_records_returns_empty_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            provider = EDINETProvider(None, cache_dir, sqlite_path=sqlite_path, cache_only=True)

            self.assertEqual(provider.load_metric_records(date(2026, 4, 24)), {})

    def test_cache_only_load_metric_records_returns_empty_with_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            provider = EDINETProvider(
                "api-key", cache_dir, sqlite_path=sqlite_path, cache_only=True
            )

            self.assertEqual(provider.load_metric_records(date(2026, 4, 24)), {})

    def test_cache_only_list_documents_raises_when_sqlite_missing_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            provider = EDINETProvider(
                "api-key", cache_dir, sqlite_path=sqlite_path, cache_only=True
            )

            with self.assertRaisesRegex(EDINETProviderError, "SQLite cache incomplete"):
                provider.list_documents(date(2026, 4, 24))


class JPXProviderReadThroughTests(unittest.TestCase):
    def test_get_regulation_snapshot_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                ("2026-04-24", "取引停止", "1302", "取引停止", "2026-04-24T00:00:00+00:00"),
            )
            _add_source_coverage(conn, source="jpx_regulation_flags", date_iso="2026-04-24")
            conn.commit()
            conn.close()

            provider = JPXProvider(cache_dir, sqlite_path=sqlite_path)

            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

            self.assertEqual(snapshot.flags_by_ticker["1302"], ("取引停止",))

    def test_has_regulation_cache_true_when_sqlite_has_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            store_jpx_regulations(
                sqlite_path,
                date(2026, 4, 24),
                flags_by_ticker={"1302": ["取引停止"]},
                source_names=["取引停止"],
            )

            provider = JPXProvider(cache_dir, sqlite_path=sqlite_path)

            self.assertTrue(provider.has_regulation_cache(date(2026, 4, 24)))

    def test_get_regulation_snapshot_falls_back_to_json_when_sqlite_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            provider = JPXProvider(cache_dir, sqlite_path=sqlite_path)

            with self.assertRaises(JPXProviderError):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_cache_only_ignores_json_regulation_cache_when_sqlite_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps({"flags_by_ticker": {"1301": ["取引停止"]}, "source_names": []}),
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir, sqlite_path=sqlite_path, cache_only=True)

            self.assertFalse(provider.has_regulation_cache(date(2026, 4, 24)))
            with self.assertRaisesRegex(JPXProviderError, "SQLite cache incomplete"):
                provider.get_regulation_snapshot(date(2026, 4, 24))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
