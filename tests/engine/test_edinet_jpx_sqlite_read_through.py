from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.market.edinet_metrics.revision import EDINET_METRIC_REVISION
from baibai_engine.market.edinet_metrics.store import read_edinet_documents, read_edinet_metrics
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import select_document_candidates
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import has_jpx_regulation_data, read_jpx_regulations
from baibai_engine.storage.observations.schema import open_connection


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
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
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
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
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
                "investment_securities, ocf_receivables_cash_effect, "
                "capex_ppe_reported, capex_intangible_reported"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    -12_000.0,
                    30_000.0,
                    None,
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
            self.assertEqual(records["1301"].ocf_receivables_cash_effect, -12_000.0)
            self.assertEqual(records["1301"].capex_ppe_reported, 30_000.0)
            self.assertIsNone(records["1301"].capex_intangible_reported)


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


class EDINETRefreshReadThroughTests(unittest.TestCase):
    def test_list_documents_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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
            retain_list_response(
                conn,
                doc_date=date(2026, 4, 24),
                received_at=datetime(2026, 4, 24, 12, tzinfo=UTC),
                body=b'{"metadata":{"resultset":{"count":1}},"results":[{"seqNumber":1,"docID":"S100B","secCode":"13010","docTypeCode":"120"}]}',
            )
            conn.close()

            provider = EDINETRefresh("api-key", sqlite_path=sqlite_path)

            with patch(
                "baibai_engine.data_collection.providers.edinet.http.requests.Session",
                side_effect=AssertionError("offline replay attempted HTTP"),
            ):
                docs = provider.list_documents(date(2026, 4, 24))

            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["docID"], "S100B")

    def test_load_metric_records_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            document = {
                "seqNumber": 1,
                "docID": "S100SAVED",
                "secCode": "13010",
                "docTypeCode": "120",
                "csvFlag": "1",
                "xbrlFlag": "1",
                "legalStatus": "1",
                "disclosureStatus": "0",
                "withdrawalStatus": "0",
                "docInfoEditStatus": "0",
                "periodStart": "2025-04-01",
                "periodEnd": "2026-03-31",
                "submitDateTime": "2026-04-01 12:00",
            }
            store_edinet_documents(sqlite_path, date(2026, 4, 24), [document])
            candidate = select_document_candidates([document]).candidates["1301"]
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
            conn.execute(
                "UPDATE edinet_metrics SET source_doc_id = ?, document_type = ?, "
                "extractor_revision = ?, source_document_revision = ?",
                (
                    candidate.doc_id,
                    candidate.doc_type_code,
                    EDINET_METRIC_REVISION,
                    candidate.source_document_revision,
                ),
            )
            conn.commit()
            conn.close()

            reader = EDINETReader(sqlite_path)

            records = reader.load_metric_records(date(2026, 4, 24))

            self.assertIn("1301", records)
            self.assertEqual(records["1301"].sales_ttm, 1_000_000.0)
            self.assertEqual(records["1301"].source_submit_datetime, "2026-04-01 12:00")
            self.assertEqual(records["1301"].snapshot_asof, date(2026, 4, 24))

    def test_reader_list_documents_raises_when_sqlite_missing_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            with self.assertRaisesRegex(EDINETProviderError, "unavailable"):
                EDINETReader(sqlite_path).list_documents(date(2026, 4, 24))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
