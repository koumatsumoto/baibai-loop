"""現行EDINET取得・再照会の回帰契約。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from itertools import count
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from tests.helpers.edinet_csv_zip import _edinet_csv_zip
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.http_doubles import (
    ExplodingSession,
    FakeResponse,
    FakeSession,
    json_response,
    response,
    sequence,
)

import baibai_jobs.maintenance.source_refresh.edinet as edinet_refresh_module
from baibai_engine.data_collection.providers.edinet import http as edinet_http
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


def _SequenceBytesSession(contents: list[bytes]) -> FakeSession:
    """Each request takes the next payload, in order."""

    return sequence(
        response(content, content_type="application/octet-stream") for content in contents
    )


def _TransientThenBytesSession(*, content: bytes, secret: str = "key") -> FakeSession:
    """One connection failure whose message carries a credential, then the payload."""

    return FakeSession(
        requests.ConnectionError(f"temporary failure Subscription-Key={secret}"),
        response(content),
    )


def _TransientThenJsonSession() -> FakeSession:
    """One connection failure, then an empty but well-formed EDINET listing."""

    return FakeSession(
        requests.ConnectionError("temporary failure"),
        json_response(
            {
                "metadata": {
                    "resultset": {"count": 0},
                    "processDateTime": "2026-07-10 12:00",
                },
                "results": [],
            }
        ),
    )


class _DateJsonSession(FakeSession):
    """Answers per requested `date=` query parameter, refusing the ones named."""

    def __init__(
        self,
        payloads: dict[str, dict[str, object]],
        *,
        failing_dates: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._payloads = payloads
        self._failing_dates = failing_dates or set()

    def get(
        self, url: str, timeout: int | None = None, *, allow_redirects: bool = True
    ) -> FakeResponse:
        del timeout, allow_redirects
        requested_date = parse_qs(urlsplit(url).query)["date"][0]
        self.calls.append(requested_date)
        if requested_date in self._failing_dates:
            return json_response({}, status_code=400)
        if requested_date == "2026-07-11" and requested_date not in self._payloads:
            return json_response({"metadata": {"resultset": {"count": 0}}, "results": []})
        return json_response(self._payloads[requested_date])


_HISTORICAL_TICKS = count()


class _HistoricalClock:
    @staticmethod
    def now(zone):
        return (
            datetime(2026, 7, 11, 12, tzinfo=UTC) + timedelta(microseconds=next(_HISTORICAL_TICKS))
        ).astimezone(zone)


class EDINETRefreshTests(unittest.TestCase):
    def test_download_csv_zip_rejects_invalid_doc_id_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = EDINETRefresh(
                "key",
                sqlite_path=Path(tmp) / "market.sqlite",
                session=ExplodingSession("request should not be attempted"),
            )

            with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET docID"):
                provider.download_csv_zip("../../etc/passwd")

    def test_download_csv_zip_ignores_old_local_non_zip_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cached = cache / "edinet/csv_zips/S100TEST.zip"
            cached.parent.mkdir(parents=True)
            cached.write_text('{"StatusCode":"429","message":"Too Many Requests"}')
            expected = _edinet_csv_zip([("jppfs_cor:NetSales", "CurrentYearDuration", "1000")])
            provider = EDINETRefresh(
                "key",
                sqlite_path=Path(tmp) / "market.sqlite",
                session=_SequenceBytesSession([expected]),
            )

            content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(
                cached.read_text(), '{"StatusCode":"429","message":"Too Many Requests"}'
            )
            self.assertEqual(
                EDINETReader(Path(tmp) / "market.sqlite").read_csv_zip("S100TEST"), expected
            )

    def test_download_csv_zip_retries_json_rate_limit_without_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = _edinet_csv_zip([("jppfs_cor:NetSales", "CurrentYearDuration", "1000")])
            provider = EDINETRefresh(
                "key",
                sqlite_path=Path(tmp) / "market.sqlite",
                session=_SequenceBytesSession(
                    [
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        expected,
                    ]
                ),
            )

            with patch("baibai_engine.data_collection.providers.edinet.http.time.sleep"):
                content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(
                EDINETReader(cache / "market.sqlite").read_csv_zip("S100TEST"), expected
            )
            self.assertFalse((cache / "edinet/csv_zips/S100TEST.zip").exists())

    def test_download_csv_zip_retries_transient_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = _edinet_csv_zip([("jppfs_cor:NetSales", "CurrentYearDuration", "1000")])
            session = _TransientThenBytesSession(content=expected)
            provider = EDINETRefresh(
                "key", sqlite_path=Path(tmp) / "market.sqlite", session=session
            )

            with patch("baibai_engine.data_collection.providers.edinet.http.time.sleep") as sleep:
                content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(len(session.calls), 2)
            sleep.assert_called_once_with(3)
            self.assertEqual(
                EDINETReader(cache / "market.sqlite").read_csv_zip("S100TEST"), expected
            )
            self.assertFalse((cache / "edinet/csv_zips/S100TEST.zip").exists())

    def test_list_documents_retries_transient_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _TransientThenJsonSession()
            provider = EDINETRefresh(
                "key", session=session, sqlite_path=Path(tmp) / "market.sqlite"
            )

            with patch("baibai_engine.data_collection.providers.edinet.http.time.sleep") as sleep:
                documents = provider.list_documents(date(2026, 7, 10))

            self.assertEqual(documents, [])
            self.assertEqual(len(session.calls), 2)
            sleep.assert_called_once_with(3)

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_refresh_document_state_fetches_target_then_all_unresolved_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            unresolved_dates = (
                date(2026, 7, 4),
                date(2026, 7, 7),
                date(2026, 7, 8),
            )
            for index, unresolved in enumerate(unresolved_dates, start=1):
                store_edinet_documents(
                    sqlite_path,
                    unresolved,
                    [{"seqNumber": index, "docID": f"S100OLD{index}"}],
                )
            target = date(2026, 7, 10)
            payloads = {
                value.isoformat(): {
                    "metadata": {
                        "resultset": {"count": 0},
                        "processDateTime": f"{value.isoformat()} 23:00",
                    },
                    "results": [],
                }
                for value in (*unresolved_dates, target)
            }
            session = _DateJsonSession(payloads)
            provider = EDINETRefresh(
                "key",
                session=session,
                sqlite_path=sqlite_path,
            )

            self.assertEqual(provider.refresh_document_state(target), {"documents": 0})
            self.assertEqual(
                session.calls,
                [
                    "2026-07-11",
                    target.isoformat(),
                    *(value.isoformat() for value in unresolved_dates),
                ],
            )
            conn = sqlite3.connect(sqlite_path)
            try:
                rechecks = dict(
                    conn.execute(
                        "SELECT doc_date, rechecked_asof FROM edinet_document_lists ORDER BY doc_date"
                    ).fetchall()
                )
            finally:
                conn.close()
            self.assertEqual(rechecks[target.isoformat()], "2026-07-11")
            self.assertTrue(
                all(rechecks[value.isoformat()] == "2026-07-11" for value in unresolved_dates)
            )

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_refresh_document_state_target_failure_does_not_recheck_prior_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            prior = date(2026, 7, 8)
            target = date(2026, 7, 10)
            store_edinet_documents(
                sqlite_path,
                prior,
                [{"seqNumber": 1, "docID": "S100PRIOR"}],
            )
            session = _DateJsonSession({}, failing_dates={target.isoformat()})
            provider = EDINETRefresh(
                "key",
                session=session,
                sqlite_path=sqlite_path,
            )

            with self.assertRaisesRegex(EdinetAcquisitionError, "status 400"):
                provider.refresh_document_state(target)
            self.assertEqual(session.calls, ["2026-07-11", target.isoformat()])
            conn = sqlite3.connect(sqlite_path)
            try:
                recheck = conn.execute(
                    "SELECT rechecked_asof FROM edinet_document_lists WHERE doc_date = ?",
                    (prior.isoformat(),),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(recheck, (None,))

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_refresh_document_state_count_mismatch_blocks_target_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            target = date(2026, 7, 10)
            store_edinet_documents(
                sqlite_path,
                target,
                [{"seqNumber": 1, "docID": "S100KEPT"}],
            )
            session = _DateJsonSession(
                {
                    target.isoformat(): {
                        "metadata": {
                            "resultset": {"count": 1},
                            "processDateTime": "2026-07-10 23:00",
                        },
                        "results": [],
                    }
                }
            )
            provider = EDINETRefresh(
                "key",
                session=session,
                sqlite_path=sqlite_path,
            )

            with self.assertRaisesRegex(EDINETProviderError, "does not match"):
                provider.refresh_document_state(target)
            conn = sqlite3.connect(sqlite_path)
            try:
                rows = conn.execute(
                    "SELECT doc_id FROM edinet_documents WHERE doc_date = ?",
                    (target.isoformat(),),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [])

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_refresh_document_state_retry_only_keeps_unfinished_prior_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            completed = date(2026, 7, 7)
            unfinished = date(2026, 7, 8)
            target = date(2026, 7, 10)
            for index, unresolved in enumerate((completed, unfinished), start=1):
                store_edinet_documents(
                    sqlite_path,
                    unresolved,
                    [{"seqNumber": index, "docID": f"S100PENDING{index}"}],
                )

            def empty_payload(value: date) -> dict[str, object]:
                return {
                    "metadata": {
                        "resultset": {"count": 0},
                        "processDateTime": f"{value.isoformat()} 23:00",
                    },
                    "results": [],
                }

            first_session = _DateJsonSession(
                {
                    target.isoformat(): empty_payload(target),
                    completed.isoformat(): empty_payload(completed),
                },
                failing_dates={unfinished.isoformat()},
            )
            provider = EDINETRefresh(
                "key",
                session=first_session,
                sqlite_path=sqlite_path,
            )
            with self.assertRaisesRegex(EdinetAcquisitionError, "status 400"):
                provider.refresh_document_state(target)
            self.assertEqual(
                first_session.calls,
                ["2026-07-11", target.isoformat(), completed.isoformat(), unfinished.isoformat()],
            )

            retry_session = _DateJsonSession(
                {
                    target.isoformat(): empty_payload(target),
                    unfinished.isoformat(): empty_payload(unfinished),
                }
            )
            retry_provider = EDINETRefresh(
                "key",
                session=retry_session,
                sqlite_path=sqlite_path,
            )
            retry_provider.refresh_document_state(target)
            self.assertEqual(
                retry_session.calls,
                ["2026-07-11", target.isoformat(), unfinished.isoformat()],
            )

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_bootstrap_and_backfill_fetch_today_proof_before_historical_days(self) -> None:
        today = date(2026, 7, 11)
        start = date(2026, 7, 8)
        end = date(2026, 7, 10)
        proof = {
            "metadata": {"resultset": {"count": 1}},
            "results": [{"seqNumber": 1, "docID": "S100PROOF"}],
        }
        empty = {"metadata": {"resultset": {"count": 0}}, "results": []}
        payloads = {today.isoformat(): proof}
        payloads.update(
            {value.isoformat(): empty for value in (start, start + timedelta(days=1), end)}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.sqlite"
            bootstrap_session = _DateJsonSession(payloads)
            bootstrap = EDINETRefresh("key", session=bootstrap_session, sqlite_path=path)
            self.assertEqual(bootstrap.bootstrap_cache(start, end), {"documents": 0})
            self.assertEqual(
                bootstrap_session.calls,
                [today.isoformat(), end.isoformat(), start.isoformat(), "2026-07-09"],
            )
            backfill_session = _DateJsonSession(payloads)
            backfill = EDINETRefresh("key", session=backfill_session, sqlite_path=path)
            self.assertEqual(
                backfill.backfill_document_identity(start, end), {"days": 3, "documents": 0}
            )
            self.assertEqual(
                backfill_session.calls,
                [today.isoformat(), start.isoformat(), "2026-07-09", end.isoformat()],
            )

    @patch.object(edinet_refresh_module, "datetime", _HistoricalClock)
    @patch.object(edinet_http, "datetime", _HistoricalClock)
    def test_current_day_refresh_does_not_claim_a_later_recheck(self) -> None:
        today = date(2026, 7, 11)
        payload = {today.isoformat(): {"metadata": {"resultset": {"count": 0}}, "results": []}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.sqlite"
            session = _DateJsonSession(payload)
            provider = EDINETRefresh("key", session=session, sqlite_path=path)
            self.assertEqual(provider.refresh_document_state(today), {"documents": 0})
            self.assertEqual(session.calls, [today.isoformat()])
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT rechecked_asof FROM edinet_document_lists WHERE doc_date = ?",
                        (today.isoformat(),),
                    ).fetchone(),
                    (None,),
                )

    def test_download_csv_zip_final_connection_error_is_redacted_without_final_sleep(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = FakeSession(default=requests.ConnectionError("temporary failure secret-key"))
            provider = EDINETRefresh(
                "secret-key", sqlite_path=Path(tmp) / "market.sqlite", session=session
            )

            with (
                patch("baibai_engine.data_collection.providers.edinet.http.time.sleep") as sleep,
                self.assertRaises(EdinetAcquisitionError) as caught,
            ):
                provider.download_csv_zip("S100TEST")

            self.assertEqual(len(session.calls), 6)
            self.assertEqual(sleep.call_count, 5)
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertIn("transport failed", str(caught.exception))

    def test_download_csv_zip_raises_rate_limit_after_json_retries_without_caching_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            provider = EDINETRefresh(
                "key",
                sqlite_path=Path(tmp) / "market.sqlite",
                session=_SequenceBytesSession(
                    [
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                    ]
                ),
            )

            with (
                patch("baibai_engine.data_collection.providers.edinet.http.time.sleep"),
                self.assertRaisesRegex(EdinetRateLimitError, "rate limited"),
            ):
                provider.download_csv_zip("S100TEST")

            self.assertFalse((cache / "edinet/csv_zips/S100TEST.zip").exists())

    def test_edinet_refresh_requires_api_key_for_unretained_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = EDINETRefresh(None, sqlite_path=Path(tmpdir) / "market.sqlite")
            with self.assertRaisesRegex(EdinetAcquisitionError, "EDINET_API_KEY"):
                provider.download_csv_zip("S100TEST")
