"""Offline metric extraction: retained baselines, revisions and filing changes."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from tests.helpers.edinet_metrics import (
    FakeEDINETMetricReader,
    _edinet_csv_zip,
    _edinet_document,
)

from baibai_engine.machine_analysis.observations.financials.edinet_metrics import (
    service as edinet_extraction,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    extract_edinet_metrics_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError


class EdinetMetricExtractionTests(unittest.TestCase):
    def test_extract_edinet_metrics_command_writes_parsed_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETMetricReader(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                        "periodStart": "2025-04-01",
                        "periodEnd": "2026-03-31",
                        "submitDateTime": "2026-04-01 12:00",
                    }
                ],
                zip_by_doc_id={"S100TEST": _edinet_csv_zip()},
            )
            buffer = io.StringIO()
            exit_code = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=provider,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            record = payload["9682"]
            self.assertEqual(record.ticker, "9682")
            self.assertEqual(record.net_cash, 600.0)
            self.assertEqual(record.fcf_ttm, 700.0)
            self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
            self.assertEqual(record.source_period_start, date(2025, 4, 1))
            self.assertEqual(record.source_period_end, date(2026, 3, 31))
            self.assertIn("selected=1 reused=0 parsed=1", buffer.getvalue())


    def test_recording_a_failure_keeps_the_snapshot_when_the_store_cannot_be_read(
        self,
    ) -> None:
        # An unreadable schema is not evidence that the day's snapshot is absent.
        # Failure recording must preserve those rows rather than attempt a replacement.
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 24),
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1000.0,
                        "ocf_ttm": 200.0,
                        "extractor_revision": "test",
                        "source_document_revision": "test",
                    }
                ],
                status="ok",
            )
            conn = sqlite3.connect(sqlite_path)
            try:
                conn.execute("PRAGMA user_version = 1")
                conn.commit()
            finally:
                conn.close()

            edinet_extraction._record_edinet_extraction_failure(
                sqlite_path=sqlite_path,
                asof_date=date(2026, 4, 24),
                message="provider unavailable",
            )

            conn = sqlite3.connect(sqlite_path)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM edinet_metrics WHERE asof_date = ?",
                    ("2026-04-24",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(remaining, 1)


    def test_extract_edinet_metrics_command_returns_zero_for_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETMetricReader(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                        "submitDateTime": "2026-04-01 12:00",
                    }
                ],
                zip_by_doc_id={"S100TEST": _edinet_csv_zip(include_debt=False)},
            )
            buffer = io.StringIO()
            exit_code = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=provider,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            self.assertIn("metric_unavailable:debt", payload["9682"].failure_reasons)
            self.assertIn("quality_issues=1", buffer.getvalue())
            reuse_provider = FakeEDINETMetricReader(documents=list(provider.documents or []))
            reuse_buffer = io.StringIO()
            reuse_exit = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 25),
                lookback_days=0,
                reader=reuse_provider,
                sqlite_path=sqlite_path,
                stdout=reuse_buffer,
            )
            self.assertEqual(reuse_exit, 0)
            self.assertEqual(reuse_provider.read_calls, [])
            self.assertIn("reused=1 parsed=0", reuse_buffer.getvalue())
            self.assertIn("quality_issues=1", reuse_buffer.getvalue())


    def test_extract_edinet_metrics_command_quarantines_affected_ticker_and_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETMetricReader(
                documents=[
                    {
                        "docID": "S100MISSING",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "docInfoEditStatus": "1",
                        "withdrawalStatus": "0",
                        "disclosureStatus": "0",
                    },
                    {
                        "docID": "S100HEALTHY",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                    },
                ],
                zip_by_doc_id={"S100HEALTHY": _edinet_csv_zip(include_debt=True)},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=provider,
                    sqlite_path=sqlite_path,
                    stdout=stdout,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.read_calls, ["S100HEALTHY"])
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            self.assertIsNone(payload["7203"].sales_ttm)
            self.assertEqual(
                payload["7203"].failure_reasons,
                ("document_event_quarantined:edit:S100MISSING",),
            )
            self.assertIsNotNone(payload["9682"].sales_ttm)
            self.assertIn("events=1 affected_tickers=1", stderr.getvalue())
            self.assertIn("S100MISSING(edit,type=120,ticker=7203)", stderr.getvalue())
            self.assertIn("quarantined_events=1 quarantined_tickers=1", stdout.getvalue())
            self.assertIn("quarantine_sample=S100MISSING:edit:120", stdout.getvalue())


    def test_extract_edinet_metrics_command_uses_baseline_to_quarantine_withdrawal_ticker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 23),
                [
                    {
                        "ticker": "7203",
                        "sales_ttm": 1000.0,
                        "source_doc_id": "S100ORIGIN",
                        "document_type": "120",
                        "extractor_revision": "seed",
                        "source_document_revision": "seed",
                    }
                ],
            )
            documents = [
                {
                    "docID": "S100WITHDRAW",
                    "parentDocID": "S100ORIGIN",
                    "docInfoEditStatus": "0",
                    "withdrawalStatus": "1",
                    "disclosureStatus": "0",
                    "legalStatus": "0",
                },
                {
                    "docID": "S100HEALTHY",
                    "secCode": "96820",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
            ]
            first_provider = FakeEDINETMetricReader(
                documents=documents,
                zip_by_doc_id={"S100HEALTHY": _edinet_csv_zip(include_debt=True)},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=first_provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            first = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert first is not None
            self.assertEqual(first["7203"].source_doc_id, "S100ORIGIN")
            self.assertEqual(first["7203"].document_type, "120")
            self.assertIsNone(first["7203"].sales_ttm)

            second_provider = FakeEDINETMetricReader(documents=documents)
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=second_provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            second = read_edinet_metrics(sqlite_path, date(2026, 4, 25))
            assert second is not None
            self.assertIsNone(second["7203"].sales_ttm)
            self.assertEqual(
                second["7203"].failure_reasons,
                ("document_event_quarantined:withdrawal:S100WITHDRAW",),
            )
            self.assertEqual(second_provider.read_calls, [])


    def test_extract_edinet_metrics_command_fails_when_no_filings_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETMetricReader(documents=[])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("no EDINET filings selected", stderr.getvalue())
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection:
                row = connection.execute(
                    "SELECT status, error FROM source_coverage WHERE source = ?",
                    ("edinet_metrics",),
                ).fetchone()
            self.assertEqual(row[0], "failed")
            self.assertIn("no EDINET filings selected", row[1])


    def test_extract_edinet_metrics_command_fails_closed_on_document_listing_error(self) -> None:
        class FailingListReader(FakeEDINETMetricReader):
            def list_documents(self, on_date: date) -> list[dict[str, object]]:
                del on_date
                raise EDINETProviderError("stored document list is unavailable")

        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
                status="ok",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    reader=FailingListReader(),
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("EDINET document listing failed", stderr.getvalue())
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection:
                row = connection.execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                ).fetchone()
            self.assertEqual(row, ("ok", None))


    def test_extract_edinet_metrics_command_fails_on_invalid_document_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
                status="ok",
            )
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            provider = FakeEDINETMetricReader(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "../../96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                    }
                ]
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    reader=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("invalid EDINET secCode", stderr.getvalue())
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection:
                row = connection.execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                ).fetchone()
            self.assertEqual(row, ("ok", None))


    def test_extract_edinet_metrics_command_fails_closed_on_missing_stored_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
                status="ok",
            )
            provider = FakeEDINETMetricReader(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                    }
                ]
            )
            stderr = io.StringIO()
            stdout = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    reader=provider,
                    sqlite_path=sqlite_path,
                    stdout=stdout,
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("hard_failures=1", stdout.getvalue())
            self.assertIn("output=not-written", stdout.getvalue())
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection:
                row = connection.execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                ).fetchone()
            self.assertEqual(row, ("ok", None))


    def test_extract_edinet_metrics_reuses_cross_asof_and_same_asof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100STABLE")
            first = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100STABLE": _edinet_csv_zip()},
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=first,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            first_snapshot = read_edinet_metrics(sqlite_path, date(2026, 4, 24))

            cross_asof = FakeEDINETMetricReader(documents=[document])
            cross_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=cross_asof,
                    sqlite_path=sqlite_path,
                    stdout=cross_output,
                ),
                0,
            )
            self.assertEqual(cross_asof.read_calls, [])
            self.assertEqual(
                read_edinet_metrics(sqlite_path, date(2026, 4, 25)),
                first_snapshot,
            )
            self.assertIn(
                "selected=1 reused=1 parsed=0 baseline_asof=2026-04-24",
                cross_output.getvalue(),
            )

            same_asof = FakeEDINETMetricReader(documents=[document])
            same_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=same_asof,
                    sqlite_path=sqlite_path,
                    stdout=same_output,
                ),
                0,
            )
            self.assertEqual(same_asof.read_calls, [])
            self.assertIn("baseline_asof=2026-04-25", same_output.getvalue())


    def test_extract_edinet_metrics_mixed_delta_matches_forced_full_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            incremental_db = root / "incremental.sqlite"
            full_db = root / "full.sqlite"
            baseline_documents = [
                _edinet_document(ticker="13010", doc_id="S100A"),
                _edinet_document(ticker="72030", doc_id="S100B"),
                _edinet_document(ticker="96820", doc_id="S100REMOVED"),
            ]
            baseline_provider = FakeEDINETMetricReader(
                documents=baseline_documents,
                zip_by_doc_id={
                    document["docID"]: _edinet_csv_zip() for document in baseline_documents
                },
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=baseline_provider,
                    sqlite_path=incremental_db,
                    stdout=io.StringIO(),
                ),
                0,
            )
            current_documents = [
                _edinet_document(ticker="13010", doc_id="S100A"),
                _edinet_document(ticker="72030", doc_id="S100BCORR", doc_type="130"),
                _edinet_document(ticker="67580", doc_id="S100NEW"),
            ]
            delta_provider = FakeEDINETMetricReader(
                documents=current_documents,
                zip_by_doc_id={
                    "S100BCORR": _edinet_csv_zip(),
                    "S100NEW": _edinet_csv_zip(),
                },
            )
            delta_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=delta_provider,
                    sqlite_path=incremental_db,
                    stdout=delta_output,
                ),
                0,
            )
            self.assertEqual(
                set(delta_provider.read_calls),
                {"S100BCORR", "S100NEW"},
            )
            self.assertIn("selected=3 reused=1 parsed=2", delta_output.getvalue())

            full_provider = FakeEDINETMetricReader(
                documents=current_documents,
                zip_by_doc_id={
                    document["docID"]: _edinet_csv_zip() for document in current_documents
                },
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=full_provider,
                    sqlite_path=full_db,
                    stdout=io.StringIO(),
                ),
                0,
            )
            incremental = read_edinet_metrics(incremental_db, date(2026, 4, 25))
            forced_full = read_edinet_metrics(full_db, date(2026, 4, 25))
            self.assertEqual(incremental, forced_full)
            assert incremental is not None
            self.assertNotIn("9682", incremental)


    def test_extract_edinet_metrics_document_edit_event_forces_reextraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            original = _edinet_document(ticker="96820", doc_id="S100SAME")
            first = FakeEDINETMetricReader(
                documents=[original],
                zip_by_doc_id={"S100SAME": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            # A sparse native operation omits unchanged metadata. Explicit nulls
            # would clear it and make the filing ineligible rather than reusable.
            edit_event = {
                "docID": "S100SAME",
                "docInfoEditStatus": "1",
                "opeDateTime": "2026-04-25 09:00",
                "seqNumber": 2,
            }
            second = FakeEDINETMetricReader(
                documents=[original, edit_event],
                zip_by_doc_id={"S100SAME": _edinet_csv_zip()},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=second,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(second.read_calls, ["S100SAME"])


    def test_extract_edinet_metrics_empty_csv_can_recover_with_repaired_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100EMPTY")
            empty_buffer = io.BytesIO()
            with zipfile.ZipFile(empty_buffer, "w") as archive:
                archive.writestr(
                    "XBRL_TO_CSV/empty.csv",
                    "要素ID\tコンテキストID\tユニットID\t値\n".encode("utf-16"),
                )
            first = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100EMPTY": empty_buffer.getvalue()},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    reader=first,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                1,
            )
            self.assertIsNone(read_edinet_metrics(sqlite_path, date(2026, 4, 24)))

            # This supplies repaired bytes explicitly. It verifies re-extraction,
            # not that ordinary acquisition replaces a retained same-ID archive.
            second = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100EMPTY": _edinet_csv_zip()},
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=second,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(second.read_calls, ["S100EMPTY"])


    def test_extract_edinet_metrics_skips_failed_baseline_and_excludes_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 25),
                [],
                status="failed",
                error="transient failure",
            )
            after_failure = FakeEDINETMetricReader(documents=[document])
            after_failure_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 26),
                    lookback_days=0,
                    reader=after_failure,
                    sqlite_path=sqlite_path,
                    stdout=after_failure_output,
                ),
                0,
            )
            self.assertEqual(after_failure.read_calls, [])
            self.assertIn("baseline_asof=2026-04-24", after_failure_output.getvalue())

            before_all_snapshots = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            before_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 23),
                    lookback_days=0,
                    reader=before_all_snapshots,
                    sqlite_path=sqlite_path,
                    stdout=before_output,
                ),
                0,
            )
            self.assertEqual(before_all_snapshots.read_calls, ["S100BASE"])
            self.assertIn("baseline_asof=none", before_output.getvalue())


    def test_extract_edinet_metrics_rejects_corrupt_latest_ok_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection, connection:
                connection.execute(
                    "UPDATE source_coverage SET record_count = 2 "
                    "WHERE source = 'edinet_metrics' AND coverage_key = '2026-04-24'"
                )
            provider = FakeEDINETMetricReader(documents=[document])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )
            self.assertEqual(exit_code, 1)
            self.assertEqual(provider.read_calls, [])
            self.assertIn("baseline is corrupt", stderr.getvalue())
            self.assertIsNone(read_edinet_metrics(sqlite_path, date(2026, 4, 25)))


    def test_extract_edinet_metrics_revision_mismatch_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                reader=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection, connection:
                connection.execute(
                    "UPDATE edinet_metrics SET extractor_revision = ?",
                    ("0" * 64,),
                )
            second = FakeEDINETMetricReader(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    reader=second,
                    sqlite_path=sqlite_path,
                    stdout=output,
                ),
                0,
            )
            self.assertEqual(second.read_calls, ["S100BASE"])
            self.assertIn("full_rebuild_reason=incompatible_revision", output.getvalue())
            with contextlib.closing(sqlite3.connect(sqlite_path)) as connection, connection:
                revision = connection.execute(
                    "SELECT extractor_revision FROM edinet_metrics WHERE asof_date = '2026-04-25'"
                ).fetchone()[0]
            self.assertEqual(revision, EDINET_METRIC_REVISION)
