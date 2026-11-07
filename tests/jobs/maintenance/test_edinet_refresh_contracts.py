"""現行EDINET取得・再照会の回帰契約。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests
from tests.helpers.edinet_csv_zip import _edinet_csv_zip
from tests.helpers.http_doubles import (
    ExplodingSession,
    FakeSession,
    json_response,
    response,
    sequence,
)

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
