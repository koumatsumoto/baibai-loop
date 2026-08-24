from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.providers.edinet import (
    EDINETProvider,
    EDINETProviderError,
    EDINETRateLimitError,
    normalize_metric_record,
    parse_doc_id,
    parse_sec_code,
    select_document_candidates,
)
from baibai_engine.screening.providers.edinet_csv import parse_csv_zip_metric_record
from baibai_engine.screening.providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProvider,
    JPXProviderError,
    _EarningsCalendarFile,
)
from baibai_engine.screening.providers.jquants import (
    JQuantsProvider,
    normalize_adjustment_factor_event,
    normalize_daily_bar,
    normalize_financial_summary,
    normalize_market_calendar,
    parse_jquants_code,
)
from baibai_engine.screening.schema import TTMQuality
from baibai_engine.screening.sqlite_cache import (
    store_edinet_documents,
    store_edinet_metrics,
    store_jpx_regulations,
)


class _FixedHtmlSession:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def get(self, url: str, timeout: int):
        del url, timeout

        class _Response:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        return _Response(self._content)


class _SequenceBytesSession:
    def __init__(self, contents: list[bytes]) -> None:
        self._contents = contents

    def get(self, url: str, timeout: int):
        del url, timeout
        content = self._contents.pop(0)

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.content = payload
                self.status_code = 200
                self.headers = {"content-type": "application/octet-stream"}

        return _Response(content)


class _TransientThenBytesSession:
    def __init__(self, *, content: bytes, secret: str = "key") -> None:
        self._content = content
        self._secret = secret
        self.calls = 0

    def get(self, url: str, timeout: int):
        del url, timeout
        self.calls += 1
        if self.calls == 1:
            raise requests.ConnectionError(f"temporary failure Subscription-Key={self._secret}")

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.content = payload
                self.status_code = 200

        return _Response(self._content)


class _TransientThenJsonSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, timeout: int):
        del url, timeout
        self.calls += 1
        if self.calls == 1:
            raise requests.ConnectionError("temporary failure")

        class _Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "metadata": {
                        "resultset": {"count": 0},
                        "processDateTime": "2026-07-10 12:00",
                    },
                    "results": [],
                }

        return _Response()


class _DateJsonSession:
    def __init__(
        self,
        payloads: dict[str, dict[str, object]],
        *,
        failing_dates: set[str] | None = None,
    ) -> None:
        self._payloads = payloads
        self._failing_dates = failing_dates or set()
        self.calls: list[str] = []

    def get(self, url: str, timeout: int):
        del timeout
        requested_date = parse_qs(urlsplit(url).query)["date"][0]
        self.calls.append(requested_date)

        class _Response:
            def __init__(self, status_code: int, payload: dict[str, object]) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self) -> dict[str, object]:
                return self._payload

        if requested_date in self._failing_dates:
            return _Response(400, {})
        return _Response(200, self._payloads[requested_date])


def _edinet_csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    csv_text = "要素ID\tコンテキストID\t値\n" + "\n".join(
        f"{element}\t{context}\t{value}" for element, context, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/sample.csv", csv_text.encode("utf-16"))
    return buffer.getvalue()


class ScreeningProviderTests(unittest.TestCase):
    def _read_jpx_fixture(self, name: str) -> bytes:
        return (ROOT / "tests" / "fixtures" / "jpx" / name).read_bytes()

    @staticmethod
    def _earnings_xlsx(rows: list[list[str]]) -> bytes:
        buffer = BytesIO()
        pd.DataFrame(rows).to_excel(buffer, index=False, header=False)
        return buffer.getvalue()

    @staticmethod
    def _earnings_url(cohort_suffix: str = "") -> str:
        return (
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
            f"att/kessan{cohort_suffix}.xlsx"
        )

    @staticmethod
    def _earnings_file(
        url: str,
        published_on: date | None,
        dates_by_ticker: dict[str, date],
        *,
        prefix: str = "att",
    ) -> _EarningsCalendarFile:
        return _EarningsCalendarFile(
            url=url.replace("/att/", f"/{prefix}/"),
            published_on=published_on,
            entries=tuple(
                JPXEarningsCalendarEntry(ticker=ticker, announcement_date=on_date)
                for ticker, on_date in dates_by_ticker.items()
            ),
            raw_record_count=len(dates_by_ticker),
            excluded_record_count=0,
        )

    def test_parse_sec_code_supports_alpha_numeric_code(self) -> None:
        self.assertEqual(parse_sec_code("130A0"), "130A")

    def test_parse_sec_code_rejects_non_zero_suffix(self) -> None:
        with self.assertRaises(EDINETProviderError):
            parse_sec_code("130A1")

    def test_parse_sec_code_sanitizes_control_characters_in_error(self) -> None:
        with self.assertRaises(EDINETProviderError) as ctx:
            parse_sec_code("7203\n0")

        self.assertNotIn("\n", str(ctx.exception))
        self.assertIn("'7203?0'", str(ctx.exception))

    def test_parse_jquants_code_supports_zero_suffix(self) -> None:
        self.assertEqual(parse_jquants_code("130A0"), "130A")

    def test_parse_jquants_code_truncates_non_zero_suffix(self) -> None:
        self.assertEqual(parse_jquants_code("130A1"), "130A")

    def test_normalize_metric_record_parses_ttm_quality(self) -> None:
        record = normalize_metric_record(
            {
                "ticker": "7203",
                "sales_ttm": "100",
                "ocf_ttm": "10",
                "debt": "20",
                "cash": "5",
                "ebitda_ttm": "30",
                "ttm_quality_ev_ebitda": "exact",
                "ttm_quality_p_s": "approximated",
                "ttm_quality_pcfr": "unavailable",
                "source_submit_datetime": "2026-04-01 12:00",
                "source_period_start": "2025-04-01",
                "source_period_end": "2026-03-31",
            }
        )
        self.assertEqual(record.ttm_quality_ev_ebitda, TTMQuality.EXACT)
        self.assertEqual(record.ttm_quality_p_s, TTMQuality.APPROXIMATED)
        self.assertEqual(record.ttm_quality_pcfr, TTMQuality.UNAVAILABLE)
        self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(record.source_period_start, date(2025, 4, 1))
        self.assertEqual(record.source_period_end, date(2026, 3, 31))

    def test_select_document_candidates_requires_csv_and_prefers_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100A",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "submitDateTime": "2026-05-01 10:00",
                },
                {
                    "docID": "S100B",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "submitDateTime": "2026-05-01 11:00",
                },
                {
                    "docID": "S100C",
                    "secCode": "67580",
                    "docTypeCode": "120",
                    "csvFlag": "0",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
                {
                    "docID": "S100D",
                    "secCode": "99840",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "0",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
            ]
        ).candidates
        self.assertEqual(selected["7203"].doc_id, "S100B")
        self.assertNotIn("6758", selected)
        self.assertNotIn("9984", selected)

    def test_select_document_candidates_rejects_invalid_sec_code(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET secCode"):
            select_document_candidates(
                [
                    {
                        "docID": "S100A",
                        "secCode": "../../72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                    }
                ]
            )

    def test_select_document_candidates_skips_missing_sec_code(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100A",
                    "secCode": None,
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
                {
                    "docID": "S100B",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
            ]
        ).candidates

        self.assertEqual(tuple(selected), ("7203",))

    def test_parse_doc_id_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET docID"):
            parse_doc_id("../../etc/passwd")

    def test_parse_doc_id_sanitizes_control_characters_in_error(self) -> None:
        with self.assertRaises(EDINETProviderError) as ctx:
            parse_doc_id("S100\nBAD")

        self.assertNotIn("\n", str(ctx.exception))
        self.assertIn("'S100?BAD'", str(ctx.exception))

    def test_download_csv_zip_rejects_invalid_doc_id_before_cache_path(self) -> None:
        class ExplodingSession:
            def get(self, url: str, timeout: int):
                del url, timeout
                raise AssertionError("request should not be attempted")

        with tempfile.TemporaryDirectory() as tmp:
            provider = EDINETProvider("key", Path(tmp), session=ExplodingSession())

            with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET docID"):
                provider.download_csv_zip("../../etc/passwd")

    def test_download_csv_zip_discards_cached_non_zip_and_refetches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cached = cache / "edinet/csv_zips/S100TEST.zip"
            cached.parent.mkdir(parents=True)
            cached.write_text('{"StatusCode":"429","message":"Too Many Requests"}')
            expected = _edinet_csv_zip(
                [("jpcrp_cor:NetSales", "CurrentYearDuration_ConsolidatedMember", "1000")]
            )
            provider = EDINETProvider("key", cache, session=_SequenceBytesSession([expected]))

            content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(cached.read_bytes(), expected)

    def test_download_csv_zip_retries_json_rate_limit_without_caching_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = _edinet_csv_zip(
                [("jpcrp_cor:NetSales", "CurrentYearDuration_ConsolidatedMember", "1000")]
            )
            provider = EDINETProvider(
                "key",
                cache,
                session=_SequenceBytesSession(
                    [
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        expected,
                    ]
                ),
            )

            with patch("baibai_engine.screening.providers.edinet.time.sleep"):
                content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual((cache / "edinet/csv_zips/S100TEST.zip").read_bytes(), expected)

    def test_download_csv_zip_retries_transient_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = _edinet_csv_zip(
                [("jpcrp_cor:NetSales", "CurrentYearDuration_ConsolidatedMember", "1000")]
            )
            session = _TransientThenBytesSession(content=expected)
            provider = EDINETProvider("key", cache, session=session)

            with patch("baibai_engine.screening.providers.edinet.time.sleep") as sleep:
                content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(session.calls, 2)
            sleep.assert_called_once_with(3)
            self.assertEqual((cache / "edinet/csv_zips/S100TEST.zip").read_bytes(), expected)

    def test_list_documents_retries_transient_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _TransientThenJsonSession()
            provider = EDINETProvider("key", Path(tmp), session=session)

            with patch("baibai_engine.screening.providers.edinet.time.sleep") as sleep:
                documents = provider.list_documents(date(2026, 7, 10))

            self.assertEqual(documents, [])
            self.assertEqual(session.calls, 2)
            sleep.assert_called_once_with(3)

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
                    is_final=False,
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
            provider = EDINETProvider(
                "key",
                Path(tmp),
                session=session,
                sqlite_path=sqlite_path,
            )

            self.assertEqual(provider.refresh_document_state(target), {"documents": 0})
            self.assertEqual(
                session.calls,
                [target.isoformat(), *(value.isoformat() for value in unresolved_dates)],
            )
            conn = sqlite3.connect(sqlite_path)
            try:
                finality = dict(
                    conn.execute(
                        "SELECT doc_date, is_final FROM edinet_document_lists ORDER BY doc_date"
                    ).fetchall()
                )
            finally:
                conn.close()
            self.assertEqual(finality[target.isoformat()], 0)
            self.assertTrue(all(finality[value.isoformat()] == 1 for value in unresolved_dates))

    def test_refresh_document_state_target_failure_does_not_finalize_prior_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            prior = date(2026, 7, 8)
            target = date(2026, 7, 10)
            store_edinet_documents(
                sqlite_path,
                prior,
                [{"seqNumber": 1, "docID": "S100PRIOR"}],
                is_final=False,
            )
            session = _DateJsonSession({}, failing_dates={target.isoformat()})
            provider = EDINETProvider(
                "key",
                Path(tmp),
                session=session,
                sqlite_path=sqlite_path,
            )

            with self.assertRaisesRegex(EDINETProviderError, "status 400"):
                provider.refresh_document_state(target)
            self.assertEqual(session.calls, [target.isoformat()])
            conn = sqlite3.connect(sqlite_path)
            try:
                finality = conn.execute(
                    "SELECT is_final FROM edinet_document_lists WHERE doc_date = ?",
                    (prior.isoformat(),),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(finality, (0,))

    def test_refresh_document_state_count_mismatch_preserves_target_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            target = date(2026, 7, 10)
            store_edinet_documents(
                sqlite_path,
                target,
                [{"seqNumber": 1, "docID": "S100KEPT"}],
                is_final=False,
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
            provider = EDINETProvider(
                "key",
                Path(tmp),
                session=session,
                sqlite_path=sqlite_path,
            )

            with self.assertRaisesRegex(EDINETProviderError, "count mismatch"):
                provider.refresh_document_state(target)
            conn = sqlite3.connect(sqlite_path)
            try:
                rows = conn.execute(
                    "SELECT doc_id FROM edinet_documents WHERE doc_date = ?",
                    (target.isoformat(),),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [("S100KEPT",)])

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
                    is_final=False,
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
            provider = EDINETProvider(
                "key",
                Path(tmp),
                session=first_session,
                sqlite_path=sqlite_path,
            )
            with self.assertRaisesRegex(EDINETProviderError, "status 400"):
                provider.refresh_document_state(target)
            self.assertEqual(
                first_session.calls,
                [target.isoformat(), completed.isoformat(), unfinished.isoformat()],
            )

            retry_session = _DateJsonSession(
                {
                    target.isoformat(): empty_payload(target),
                    unfinished.isoformat(): empty_payload(unfinished),
                }
            )
            retry_provider = EDINETProvider(
                "key",
                Path(tmp),
                session=retry_session,
                sqlite_path=sqlite_path,
            )
            retry_provider.refresh_document_state(target)
            self.assertEqual(
                retry_session.calls,
                [target.isoformat(), unfinished.isoformat()],
            )

    def test_download_csv_zip_final_connection_error_is_redacted_without_final_sleep(
        self,
    ) -> None:
        class AlwaysFailingSession:
            calls = 0

            def get(self, url: str, timeout: int):
                del url, timeout
                self.calls += 1
                raise requests.ConnectionError("temporary failure secret-key")

        with tempfile.TemporaryDirectory() as tmp:
            session = AlwaysFailingSession()
            provider = EDINETProvider("secret-key", Path(tmp), session=session)

            with (
                patch("baibai_engine.screening.providers.edinet.time.sleep") as sleep,
                self.assertRaises(EDINETProviderError) as caught,
            ):
                provider.download_csv_zip("S100TEST")

            self.assertEqual(session.calls, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertIn("<redacted>", str(caught.exception))

    def test_download_csv_zip_raises_rate_limit_after_json_retries_without_caching_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            provider = EDINETProvider(
                "key",
                cache,
                session=_SequenceBytesSession(
                    [
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                        b'{"StatusCode":"429","message":"Too Many Requests"}',
                    ]
                ),
            )

            with (
                patch("baibai_engine.screening.providers.edinet.time.sleep"),
                self.assertRaisesRegex(EDINETRateLimitError, "rate limited"),
            ):
                provider.download_csv_zip("S100TEST")

            self.assertFalse((cache / "edinet/csv_zips/S100TEST.zip").exists())

    def test_select_document_candidates_prefers_new_period_over_old_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100OLD",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2024-04-01",
                    "periodEnd": "2025-03-31",
                    "submitDateTime": "2025-07-01 10:00",
                },
                {
                    "docID": "S100NEW",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2025-09-30",
                    "submitDateTime": "2025-11-01 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NEW")

    def test_select_document_candidates_folds_disclosure_and_release_events(self) -> None:
        origin = {
            "doc_date": "2026-04-01",
            "seqNumber": 1,
            "docID": "S100TEST",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "2",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "submitDateTime": "2026-04-01 10:00",
        }
        hidden = {
            **origin,
            "doc_date": "2026-04-02",
            "opeDateTime": "2026-04-02 12:00",
            "disclosureStatus": "1",
        }
        released = {
            **origin,
            "doc_date": "2026-04-03",
            "opeDateTime": "2026-04-03 12:00",
            "disclosureStatus": "3",
        }

        self.assertEqual(select_document_candidates([origin, hidden]).candidates, {})
        selected = select_document_candidates([released, hidden, origin]).candidates
        self.assertEqual(selected["7203"].doc_id, "S100TEST")

    def test_select_document_candidates_tombstones_withdrawn_parent_and_child(self) -> None:
        parent = {
            "doc_date": "2026-04-01",
            "seqNumber": 1,
            "docID": "S100PARENT",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        child = {
            **parent,
            "seqNumber": 2,
            "docID": "S100CHILD",
            "docTypeCode": "130",
            "parentDocID": "S100PARENT",
        }
        withdrawal = {
            "doc_date": "2026-04-02",
            "seqNumber": 1,
            "docID": "S100WITHDRAW",
            "parentDocID": "S100PARENT",
            "withdrawalStatus": "1",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "legalStatus": "0",
        }

        self.assertEqual(select_document_candidates([child, withdrawal, parent]).candidates, {})

    def test_select_document_candidates_reports_an_event_without_origin(self) -> None:
        selection = select_document_candidates(
            [
                {
                    "doc_date": "2026-04-02",
                    "seqNumber": 1,
                    "docID": "S100MISSING",
                    "secCode": "72030",
                    "docTypeCode": "120; injected",
                    "docInfoEditStatus": "1",
                    "withdrawalStatus": "0",
                    "disclosureStatus": "0",
                    "opeDateTime": "2026-04-02 12:00",
                }
            ]
        )

        self.assertEqual(selection.candidates, {})
        self.assertEqual(
            tuple(event.doc_id for event in selection.quarantined_events),
            ("S100MISSING",),
        )
        self.assertEqual(selection.quarantined_events[0].ticker, "7203")
        self.assertIsNone(selection.quarantined_events[0].document_type)

    def test_select_document_candidates_reports_a_withdrawal_without_parent(self) -> None:
        selection = select_document_candidates(
            [
                {
                    "doc_date": "2026-04-02",
                    "seqNumber": 1,
                    "docID": "S100WITHDRAW",
                    "parentDocID": "S100GONE",
                    "withdrawalStatus": "1",
                    "docInfoEditStatus": "0",
                    "disclosureStatus": "0",
                    "legalStatus": "0",
                }
            ]
        )

        self.assertEqual(selection.candidates, {})
        self.assertEqual(
            tuple(event.doc_id for event in selection.quarantined_events),
            ("S100WITHDRAW",),
        )

    def test_select_document_candidates_drops_an_event_on_an_origin_outside_lookback(self) -> None:
        origin = {
            "doc_date": "2024-01-01",
            "seqNumber": 1,
            "docID": "S100OLD",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        event = {
            **origin,
            "doc_date": "2026-04-02",
            "docInfoEditStatus": "1",
            "opeDateTime": "2026-04-02 12:00",
        }
        recent = {
            **origin,
            "doc_date": "2026-04-03",
            "docID": "S100NEW",
            "secCode": "99840",
        }

        selection = select_document_candidates(
            [origin, event, recent],
            origin_start=date(2025, 1, 1),
        )

        self.assertEqual(
            tuple(event.doc_id for event in selection.quarantined_events),
            ("S100OLD",),
        )
        self.assertEqual(selection.quarantined_events[0].event_type, "edit")
        self.assertEqual(selection.quarantined_events[0].document_type, "120")
        self.assertEqual(selection.quarantined_events[0].ticker, "7203")
        self.assertEqual(sorted(selection.candidates), ["9984"])

    def test_select_document_candidates_rejects_parent_cycle_without_withdrawal(self) -> None:
        base = {
            "doc_date": "2026-04-01",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        with self.assertRaisesRegex(EDINETProviderError, "parent relation cycle"):
            select_document_candidates(
                [
                    {**base, "seqNumber": 1, "docID": "S100A", "parentDocID": "S100B"},
                    {**base, "seqNumber": 2, "docID": "S100B", "parentDocID": "S100A"},
                ]
            )

    def test_select_document_candidates_rejects_event_after_withdrawal(self) -> None:
        origin = {
            "doc_date": "2026-04-01",
            "seqNumber": 1,
            "docID": "S100ORIGIN",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        withdrawal = {
            "doc_date": "2026-04-02",
            "seqNumber": 1,
            "docID": "S100WITHDRAW",
            "parentDocID": "S100ORIGIN",
            "withdrawalStatus": "1",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "legalStatus": "0",
            "opeDateTime": "2026-04-02 10:00",
        }
        later_edit = {
            **origin,
            "doc_date": "2026-04-03",
            "docInfoEditStatus": "1",
            "opeDateTime": "2026-04-03 10:00",
        }

        with self.assertRaisesRegex(EDINETProviderError, "event follows withdrawal"):
            select_document_candidates([later_edit, origin, withdrawal])

    def test_select_document_candidates_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "unknown EDINET legal status"):
            select_document_candidates(
                [
                    {
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "9",
                    }
                ]
            )

    def test_select_document_candidates_rejects_unknown_withdrawal_status(self) -> None:
        """The merge keeps the later reading of this column, so its domain has to hold."""

        with self.assertRaisesRegex(EDINETProviderError, "unknown EDINET withdrawal status"):
            select_document_candidates(
                [
                    {
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "withdrawalStatus": "9",
                    }
                ]
            )

    def test_select_document_candidates_prefers_same_period_annual_correction_from_description(
        self,
    ) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2024-10-01",
                    "periodEnd": "2025-09-30",
                    "submitDateTime": "2025-12-24 16:11",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正有価証券報告書－第28期(2024/10/01－2025/09/30)",
                    "submitDateTime": "2026-04-15 16:01",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].period_start, date(2024, 10, 1))
        self.assertEqual(selected["7203"].period_end, date(2025, 9, 30))

    def test_select_document_candidates_keeps_newer_period_over_old_correction_description(
        self,
    ) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100OLD",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正有価証券報告書－第90期(2024/04/01－2025/03/31)",
                    "submitDateTime": "2026-01-23 14:35",
                },
                {
                    "docID": "S100NEW",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2025-11-01 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NEW")

    def test_select_document_candidates_accepts_semiannual_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2025-11-14 15:34",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "170",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正半期報告書－第5期(2025/04/01-2026/03/31)",
                    "submitDateTime": "2026-01-30 15:33",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].doc_type_code, "170")

    def test_select_document_candidates_accepts_quarterly_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "140",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2021-11-01",
                    "periodEnd": "2022-01-31",
                    "submitDateTime": "2022-03-15 15:00",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "150",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正四半期報告書－第72期第3四半期(2021/11/01～2022/01/31)",
                    "submitDateTime": "2025-02-14 15:29",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].doc_type_code, "150")

    def test_select_document_candidates_prefers_latest_submit_within_same_period(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2026-06-01 10:00",
                },
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2026-06-02 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NORMAL")

    def test_parse_csv_zip_metric_record_extracts_net_cash_and_fcf(self) -> None:
        rows = [
            ("jpcrp_cor:NetSales", "CurrentYearConsolidatedDuration", "1000"),
            (
                "jpcrp_cor:CashFlowsFromOperatingActivities",
                "CurrentYearConsolidatedDuration",
                "150",
            ),
            ("jpcrp_cor:OperatingProfit", "CurrentYearConsolidatedDuration", "90"),
            ("jpcrp_cor:DepreciationAndAmortization", "CurrentYearConsolidatedDuration", "30"),
            (
                "jpcrp_cor:PurchaseOfPropertyPlantAndEquipment",
                "CurrentYearConsolidatedDuration",
                "-40",
            ),
            ("jpcrp_cor:CashAndDeposits", "CurrentYearConsolidatedInstant", "300"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearConsolidatedInstant", "250"),
            ("jpcrp_cor:ShortTermBorrowings", "CurrentYearConsolidatedInstant", "20"),
            ("jpcrp_cor:LongTermBorrowings", "CurrentYearConsolidatedInstant", "50"),
            ("jpcrp_cor:Equity", "CurrentYearConsolidatedInstant", "800"),
            ("jpcrp_cor:TotalAssets", "CurrentYearConsolidatedInstant", "1400"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
            submit_datetime="2026-04-01 12:00",
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
        )

        self.assertEqual(record.sales_ttm, 1000.0)
        self.assertEqual(record.ocf_ttm, 150.0)
        self.assertEqual(record.debt, 70.0)
        self.assertEqual(record.cash, 300.0)
        self.assertEqual(record.net_cash, 230.0)
        self.assertEqual(record.investment_securities, 250.0)
        self.assertEqual(record.capex_ttm, 40.0)
        self.assertEqual(record.fcf_ttm, 110.0)
        self.assertEqual(record.ebitda_ttm, 120.0)
        self.assertEqual(record.ttm_quality_fcf, TTMQuality.EXACT)
        self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(record.source_period_start, date(2025, 4, 1))
        self.assertEqual(record.source_period_end, date(2026, 3, 31))

    def test_investment_securities_uses_exact_tag_and_consolidated_basis(self) -> None:
        rows = [
            ("jppfs_cor:InvestmentSecurities", "CurrentYearConsolidatedInstant", "250"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearNonConsolidatedInstant", "900"),
            (
                "jppfs_cor:SharesOfSubsidiariesAndAssociates",
                "CurrentYearConsolidatedInstant",
                "700",
            ),
            (
                "jpcrp_cor:OperationalInvestmentSecurities",
                "CurrentYearConsolidatedInstant",
                "800",
            ),
            (
                "jppfs_cor:GainOnSaleOfInvestmentSecurities",
                "CurrentYearConsolidatedDuration",
                "1000",
            ),
        ]

        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.investment_securities, 250.0)
        self.assertNotIn("tag_not_found:investment_securities", record.failure_reasons)

    def test_investment_securities_excludes_similar_tags_and_accepts_reported_zero(self) -> None:
        excluded = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(
                [
                    (
                        "jpcrp_cor:OperationalInvestmentSecurities",
                        "CurrentYearConsolidatedInstant",
                        "800",
                    ),
                    (
                        "jppfs_cor:ProceedsFromSaleOfInvestmentSecurities",
                        "CurrentYearConsolidatedDuration",
                        "900",
                    ),
                ]
            ),
        )
        zero = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100ZERO",
            doc_type_code="120",
            content=_edinet_csv_zip(
                [("jppfs_cor:InvestmentSecurities", "CurrentYearConsolidatedInstant", "－")]
            ),
        )

        self.assertIsNone(excluded.investment_securities)
        self.assertIn("tag_not_found:investment_securities", excluded.failure_reasons)
        self.assertEqual(zero.investment_securities, 0.0)

    def test_investment_securities_rejects_negative_value(self) -> None:
        for value in ("-1", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_csv_zip_metric_record(
                    ticker="7203",
                    doc_id="S100INVALID",
                    doc_type_code="120",
                    content=_edinet_csv_zip(
                        [
                            (
                                "jppfs_cor:InvestmentSecurities",
                                "CurrentYearConsolidatedInstant",
                                value,
                            )
                        ]
                    ),
                )

    def test_parse_csv_zip_metric_record_extracts_loan_payable_and_lease_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant_ConsolidatedMember", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant_ConsolidatedMember", "120"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant_ConsolidatedMember", "30"),
            ("jppfs_cor:LeaseAssetsPPE", "CurrentYearInstant_ConsolidatedMember", "999"),
            ("jppfs_cor:LongTermLoansReceivable", "CurrentYearInstant_ConsolidatedMember", "888"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.debt, 150.0)
        self.assertEqual(record.net_cash, 850.0)
        self.assertNotIn("debt_assumed_zero", record.failure_reasons)

    def test_parse_csv_zip_metric_record_does_not_strict_quality_assumed_zero_debt(
        self,
    ) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant_ConsolidatedMember", "1000"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertIsNone(record.debt)
        self.assertIsNone(record.net_cash)
        self.assertEqual(record.ttm_quality_net_cash, TTMQuality.UNAVAILABLE)
        self.assertIn("debt_assumed_zero", record.failure_reasons)

    def test_parse_csv_zip_metric_record_allows_reported_zero_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant_ConsolidatedMember", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant_ConsolidatedMember", "－"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant_ConsolidatedMember", "－"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.debt, 0.0)
        self.assertEqual(record.net_cash, 1000.0)
        self.assertEqual(record.ttm_quality_net_cash, TTMQuality.EXACT)
        self.assertNotIn("debt_assumed_zero", record.failure_reasons)

    def test_edinet_provider_reads_cached_metrics_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / ".cache" / "screening"
            sqlite_path = Path(tmpdir) / "data" / "screening" / "market.sqlite"
            store_edinet_metrics(
                sqlite_path,
                date(2026, 5, 1),
                [
                    {
                        "ticker": "7203",
                        "cash": 100.0,
                        "debt": 10.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
            )

            provider = EDINETProvider(None, cache_dir, sqlite_path=sqlite_path)
            records = provider.load_metric_records(date(2026, 5, 1))

        self.assertEqual(records["7203"].cash, 100.0)
        self.assertEqual(records["7203"].debt, 10.0)

    def test_edinet_provider_requires_api_key_for_download_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = EDINETProvider(None, Path(tmpdir))
            with self.assertRaisesRegex(EDINETProviderError, "EDINET_API_KEY"):
                provider.download_csv_zip("S100TEST")

    def test_normalize_market_calendar_business_day(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24", "HolidayDivision": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_supports_client_v2_short_key(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24T00:00:00", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_treats_half_day_as_business(self) -> None:
        # HolidayDivision="2" は半日営業 (大納会など取引あり) で、business_day 扱いすべき。
        calendar_day = normalize_market_calendar({"Date": "2026-12-30", "HolDiv": "2"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_daily_bar_preserves_zero_turnover(self) -> None:
        # `or` チェーンで 0.0 を欠損誤判定しないことを固定化する。
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 0.5,
                "Va": 0.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.turnover_value, 0.0)

    def test_normalize_financial_summary_preserves_zero_fields(self) -> None:
        # EPS=0 / OP=0 / Sales=0 が None に潰れないこと (損益分岐点ちょうど、赤字転換など)。
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "10.0",
                "EPS": "0.0",
                "BPS": "1200.0",
                "Sales": "0",
                "OP": "0",
                "ShOutFY": "1000000",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.eps_ttm, 0.0)
        self.assertEqual(summary.sales, 0.0)
        self.assertEqual(summary.operating_profit, 0.0)

    def test_normalize_daily_bar_supports_client_v2_short_keys(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 5070.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.ticker, "1301")
        self.assertEqual(bar.close, 5070.0)
        self.assertEqual(bar.turnover_value, 526318500.0)

    def test_normalize_daily_bar_skips_non_common_suffix(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "25935",
                "Date": "2026-04-24T00:00:00",
                "C": 100.0,
            }
        )
        self.assertIsNone(bar)

    def test_normalize_daily_bar_preserves_adjustment_close(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "Close": 200.0,
                "AdjustmentClose": 100.0,
                "AdjustmentFactor": 0.5,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 200.0)
        self.assertEqual(bar.adjustment_close, 100.0)
        self.assertEqual(bar.adjustment_factor, 0.5)

    def test_normalize_daily_bar_leaves_adjustment_close_none_when_missing(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 100.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 100.0)
        self.assertIsNone(bar.adjustment_close)

    def test_normalize_adjustment_event_does_not_require_close(self) -> None:
        event = normalize_adjustment_factor_event(
            {
                "Code": "67310",
                "Date": "2023-12-27T00:00:00",
                "C": None,
                "AdjustmentFactor": 100.0,
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.ticker, "6731")
        self.assertEqual(event.adjustment_factor, 100.0)

    def test_normalize_financial_summary_supports_client_v2_short_keys(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "377.75",
                "EPS": "300.50",
                "BPS": "1200.0",
                "ShOutFY": "1000000",
                "Sales": "31300000000",
                "OP": "1840000000",
                "OdP": "1720000000",
                "NP": "1070000000",
                "CurPerType": "1Q",
                "CurFYEn": "2027-03-31T00:00:00",
                "CurPerSt": "2026-04-01T00:00:00",
                "CurPerEn": "2026-06-30T00:00:00",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.ticker, "8891")
        self.assertEqual(summary.forecast_eps, 377.75)
        self.assertEqual(summary.eps_ttm, 300.50)
        self.assertEqual(summary.shares_outstanding, 1000000.0)
        self.assertEqual(summary.operating_profit, 1840000000.0)
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_normalize_financial_summary_falls_back_to_next_year_forecast(self) -> None:
        # Full-year (FY) disclosures leave the current-period FEPS empty and carry the
        # new-year guidance in NxFEPS; forecast_eps (the basis for per_forward) must
        # still populate from the next-year forecast.
        fy = normalize_financial_summary(
            {
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": "",
                "NxFEPS": "360.26",
                "EPS": "349.18",
            }
        )
        assert fy is not None
        self.assertEqual(fy.forecast_eps, 360.26)
        self.assertEqual(fy.eps_ttm, 349.18)

    def test_non_finite_current_forecasts_do_not_cross_into_next_period(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": float("nan"),
                "NxFEPS": "120.0",
                "FNP": float("inf"),
                "NxFNp": "2000000000",
                "FOdP": float("nan"),
                "NxFOdP": "1800000000",
                "FDivAnn": float("-inf"),
                "NxFDivAnn": "12.0",
                "Sales": float("inf"),
                "CFO": float("-inf"),
                "TA": float("nan"),
                "ShOutFY": float("inf"),
                "TrShFY": float("-inf"),
                "EqAR": float("nan"),
            }
        )

        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)
        self.assertIsNone(summary.sales)
        self.assertIsNone(summary.cfo)
        self.assertIsNone(summary.total_assets)
        self.assertIsNone(summary.shares_outstanding)
        self.assertIsNone(summary.treasury_shares)
        self.assertIsNone(summary.equity_to_asset_ratio)

    def test_pandas_missing_current_forecasts_do_not_cross_periods(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": pd.NA,
                "NxFEPS": "120.0",
                "FNP": pd.NA,
                "NxFNp": "2000000000",
                "FOdP": pd.NA,
                "NxFOdP": "1800000000",
                "FDivAnn": pd.NA,
                "NxFDivAnn": "12.0",
            }
        )

        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)

    def test_normalize_financial_summary_keeps_zero_current_forecasts(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": 0,
                "NxFEPS": "120.0",
                "FNP": "1000000000",
                "NxFNp": "2000000000",
                "FOdP": "900000000",
                "NxFOdP": "1800000000",
                "FDivAnn": 0,
                "NxFDivAnn": "12.0",
            }
        )

        assert summary is not None
        self.assertEqual(summary.forecast_eps, 0.0)
        self.assertEqual(summary.forecast_profit, 1_000_000_000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 900_000_000.0)
        self.assertEqual(summary.dps_forecast_annual, 0.0)

    def test_normalize_financial_summary_keeps_average_shares_separate(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "41670",
                "DiscDate": "2024-08-14T00:00:00",
                "AvgSh": "7563857",
                "TrShFY": "352373",
            }
        )

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertIsNone(summary.shares_outstanding)
        self.assertEqual(summary.average_shares, 7_563_857.0)
        self.assertEqual(summary.treasury_shares, 352_373.0)

    def test_normalize_financial_summary_prefers_current_forecast_over_next_year(self) -> None:
        # Mid-year (Q1-Q3) disclosures carry the current-FY forecast in FEPS; it takes
        # priority over NxFEPS so forecast_eps stays the nearest forecast.
        q3 = normalize_financial_summary(
            {
                "Code": "97150",
                "DiscDate": "2026-02-13T00:00:00",
                "FEPS": "300.0",
                "NxFEPS": "145.0",
            }
        )
        assert q3 is not None
        self.assertEqual(q3.forecast_eps, 300.0)

    def test_normalize_financial_summary_reads_current_forecast_profit_pair(self) -> None:
        # 当期予想 EPS(FEPS)が埋まる四半期開示では当期予想の純利益/経常(FNP/FOdP)を採る。
        # 翌期キー(NxFNp/NxFOdP)が併存しても当期側を採り、期をまたがない。
        summary = normalize_financial_summary(
            {
                "Code": "48490",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": "138.72",
                "NxFEPS": "120.0",
                "FNP": "5464000000",
                "FOdP": "3406000000",
                "NxFNp": "900000000",
                "NxFOdP": "1200000000",
            }
        )
        assert summary is not None
        self.assertEqual(summary.forecast_profit, 5464000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 3406000000.0)

    def test_normalize_financial_summary_pairs_forecast_profit_with_next_year_on_fy(self) -> None:
        # 本決算開示で FEPS が空なら forecast_eps は NxFEPS に落ちる。純利益/経常も
        # 同じ翌期ペア(NxFNp/NxFOdP)を採り、当期の FNP/FOdP と期をまたいで比較しない
        # (期ズレ比較は一時益 flag の誤検出になる)。
        summary = normalize_financial_summary(
            {
                "Code": "48490",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "",
                "NxFEPS": "120.0",
                "FNP": "5464000000",
                "FOdP": "3406000000",
                "NxFNp": "2000000000",
                "NxFOdP": "2500000000",
            }
        )
        assert summary is not None
        self.assertEqual(summary.forecast_eps, 120.0)
        self.assertEqual(summary.forecast_profit, 2000000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 2500000000.0)

    def test_normalize_financial_summary_preserves_fiscal_period_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DisclosedDate": "2026-04-01",
                "TypeOfCurrentPeriod": "1Q",
                "CurrentFiscalYearEndDate": "2027-03-31",
                "CurrentPeriodStartDate": "2026-04-01",
                "CurrentPeriodEndDate": "2026-06-30",
                "EarningsPerShare": "300.50",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_normalize_financial_summary_skips_non_common_suffix(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "25935",
                "DiscDate": "2026-04-24T00:00:00",
                "EPS": "10.0",
            }
        )
        self.assertIsNone(summary)

    def test_get_eq_bars_daily_range_chunks_long_windows(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_eq_bars_daily_range(
                self, start_dt: str, end_dt: str
            ) -> list[dict[str, object]]:
                self.calls.append((start_dt, end_dt))
                # Dense bars per day so a fetched chunk satisfies the
                # data-derived coverage check on read-back.
                current = date.fromisoformat(start_dt)
                stop = date.fromisoformat(end_dt)
                rows: list[dict[str, object]] = []
                while current <= stop:
                    rows.append(
                        {
                            "Code": "13010",
                            "Date": f"{current.isoformat()}T00:00:00",
                            "C": 100.0,
                            "Va": 1000.0,
                        }
                    )
                    current += timedelta(days=1)
                return rows

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "data" / "screening" / "market.sqlite"
            provider = JQuantsProvider("token", Path(tmp), client=client, sqlite_path=sqlite_path)
            with patch("baibai_engine.market.provider.time.sleep", return_value=None):
                bars = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], ("2026-01-01", "2026-01-31"))
        self.assertEqual(client.calls[1], ("2026-02-01", "2026-02-15"))
        self.assertGreater(len(bars), 2)

    def test_get_eq_bars_daily_range_skips_sleep_on_cache_hit(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_eq_bars_daily_range(
                self, start_dt: str, end_dt: str
            ) -> list[dict[str, object]]:
                self.calls.append((start_dt, end_dt))
                # Dense bars per day so a fetched chunk satisfies the
                # data-derived coverage check on read-back.
                current = date.fromisoformat(start_dt)
                stop = date.fromisoformat(end_dt)
                rows: list[dict[str, object]] = []
                while current <= stop:
                    rows.append(
                        {
                            "Code": "13010",
                            "Date": f"{current.isoformat()}T00:00:00",
                            "C": 100.0,
                            "Va": 1000.0,
                        }
                    )
                    current += timedelta(days=1)
                return rows

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "data" / "screening" / "market.sqlite"
            provider = JQuantsProvider("token", Path(tmp), client=client, sqlite_path=sqlite_path)
            with patch("baibai_engine.market.provider.time.sleep") as sleep_first:
                first = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(sleep_first.call_count, 1)

            with patch("baibai_engine.market.provider.time.sleep") as sleep_second:
                second = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(sleep_second.call_count, 0)
            self.assertEqual(len(second), len(first))

    def test_get_eq_bars_daily_range_retries_429(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_eq_bars_daily_range(
                self, start_dt: str, end_dt: str
            ) -> list[dict[str, object]]:
                del start_dt, end_dt
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("too many 429 error responses")
                return [{"Code": "13010", "Date": "2026-04-24T00:00:00", "C": 100.0, "Va": 1000.0}]

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            provider = JQuantsProvider("token", Path(tmp), client=client)
            with patch("baibai_engine.market.provider.time.sleep", return_value=None):
                bars = provider.get_eq_bars_daily_range(date(2026, 4, 24), date(2026, 4, 24))

        self.assertEqual(client.calls, 3)
        self.assertEqual(len(bars), 1)

    def test_short_sale_refresh_rechecks_covered_disclosure_dates(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_mkt_short_sale_report(self, disclosed_date: str) -> list[dict[str, object]]:
                self.calls += 1
                return [
                    {
                        "DiscDate": disclosed_date,
                        "CalcDate": disclosed_date,
                        "Code": "72030",
                        "SSName": "Reporter A",
                        "DICName": "",
                        "FundName": "",
                        "ShrtPosToSO": 0.006 if self.calls == 1 else 0.007,
                    }
                ]

        client = FakeClient()
        target = date(2026, 4, 24)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            provider = JQuantsProvider("token", Path(tmp), client=client, sqlite_path=sqlite_path)
            first = provider.get_mkt_short_sale_report_range(target, target)
            refreshed = provider.refresh_mkt_short_sale_report_range(target, target)

        self.assertEqual(client.calls, 2)
        self.assertEqual(first[0].short_ratio, 0.006)
        self.assertEqual(refreshed[0].short_ratio, 0.007)

    def test_jpx_parse_csv_rows_supports_cp932(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_csv_rows(
            "コード,規制区分\n3856,整理銘柄\n".encode("cp932"), "https://example.com/sample.csv"
        )
        self.assertEqual(rows, [{"コード": "3856", "規制区分": "整理銘柄"}])

    def test_jpx_earnings_parser_detects_header_and_normalizes_rows(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        content = self._earnings_xlsx(
            [
                ["決算発表予定", "", ""],
                [
                    "決算発表予定日\nScheduled Dates for Earnings Announcements",
                    "コード\nCode",
                    "会社名\nCompany Name",
                ],
                ["2026-07-15", "130A", "Alpha"],
                ["未定_Undecided", "7203", "Toyota"],
                ["", "※注", ""],
                ["2026/07/16", "72030", "Toyota"],
            ]
        )

        parsed = provider._parse_earnings_calendar_excel(
            content,
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx",
        )

        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in parsed.entries],
            [("130A", date(2026, 7, 15)), ("7203", date(2026, 7, 16))],
        )
        self.assertEqual(parsed.raw_record_count, 3)
        self.assertEqual(parsed.excluded_record_count, 1)
        self.assertIsNone(parsed.published_on)

    def test_jpx_earnings_parser_fails_on_layout_and_invalid_date(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        with self.assertRaisesRegex(JPXProviderError, "header layout"):
            provider._parse_earnings_calendar_excel(
                self._earnings_xlsx([["Date", "Code"], ["2026-07-15", "130A"]]), url
            )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX earnings date"):
            provider._parse_earnings_calendar_excel(
                self._earnings_xlsx([["決算発表日", "コード"], ["2026-99-99", "130A"]]),
                url,
            )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
            provider._parse_earnings_calendar_excel(
                self._earnings_xlsx([["決算発表日", "コード"], ["2026-07-15", "INVALID"]]),
                url,
            )

    def test_jpx_earnings_snapshot_fails_when_every_date_is_past(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/a/kessan.xlsx"
        with (
            patch.object(provider, "_resolve_earnings_calendar_urls", return_value=(url,)),
            patch.object(
                provider,
                "_download_earnings_calendar",
                return_value=self._earnings_file(url, None, {"130A": date(2026, 7, 13)}),
            ),
            self.assertRaisesRegex(JPXProviderError, "only past dates"),
        ):
            provider.get_earnings_calendar_snapshot(date(2026, 7, 14))

    def _merge_earnings_files(
        self, files: tuple[_EarningsCalendarFile, ...], asof_date: date
    ) -> JPXEarningsCalendarSnapshot:
        """Merge the given cohort files under every index ordering.

        The index order is JPX's to choose, so a snapshot that depends on it is
        a defect. This asserts the invariant and returns the one snapshot.
        """
        by_url = {source.url: source for source in files}
        snapshots: list[JPXEarningsCalendarSnapshot] = []
        for urls in (tuple(by_url), tuple(reversed(tuple(by_url)))):
            provider = JPXProvider(Path("/tmp"))
            with (
                patch.object(provider, "_resolve_earnings_calendar_urls", return_value=urls),
                patch.object(
                    provider, "_download_earnings_calendar", side_effect=lambda url: by_url[url]
                ),
            ):
                snapshots.append(provider.get_earnings_calendar_snapshot(asof_date))
        self.assertEqual(snapshots[0].entries, snapshots[1].entries)
        return snapshots[0]

    def test_jpx_earnings_snapshot_keeps_the_freshest_publication_on_conflict(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 15)}),
                self._earnings_file(
                    self._earnings_url("06_0731"),
                    date(2026, 7, 30),
                    {"130A": date(2026, 7, 16), "7203": date(2026, 7, 21)},
                ),
                self._earnings_file(
                    self._earnings_url("05_0703"),
                    date(2026, 7, 2),
                    {"7203": date(2026, 7, 22), "6758": date(2026, 7, 23)},
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in snapshot.entries],
            [
                ("130A", date(2026, 7, 15)),
                ("7203", date(2026, 7, 21)),
                ("6758", date(2026, 7, 23)),
            ],
        )
        self.assertEqual(snapshot.superseded_record_count, 2)
        self.assertEqual(snapshot.raw_record_count, 5)

    def test_jpx_earnings_snapshot_keeps_the_rolling_file_above_an_unstamped_archive(self) -> None:
        # A stamp JPX stops writing in a form we parse must not promote the
        # archive to "current"; precedence identifies the rolling file by name.
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 15)}),
                self._earnings_file(
                    self._earnings_url("06_0731"),
                    None,
                    {"130A": date(2026, 7, 16)},
                    # Sorts ahead of the rolling URL, so only the rolling rank can
                    # be what puts the rolling date on top.
                    prefix="a",
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 15))

    def test_jpx_earnings_snapshot_keeps_a_dated_archive_above_an_unstamped_one(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(
                    self._earnings_url("06_0731"), date(2026, 7, 30), {"130A": date(2026, 7, 15)}
                ),
                self._earnings_file(
                    self._earnings_url("05_0703"), None, {"130A": date(2026, 7, 16)}
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 15))

    def test_jpx_earnings_snapshot_ignores_a_stale_file_with_no_forward_dates(self) -> None:
        # A rolling file JPX failed to refresh holds only past dates and must not
        # blank out a still-scheduled announcement.
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 10)}),
                self._earnings_file(
                    self._earnings_url("06_0731"), date(2026, 7, 9), {"130A": date(2026, 7, 20)}
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 20))

    def test_jpx_earnings_snapshot_resolves_files_it_cannot_rank_the_same_way(self) -> None:
        # Two archives stamped the same day cannot be separated by publication,
        # so the snapshot must still not depend on the index listing order.
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(
                    self._earnings_url("06_0715"), date(2026, 7, 15), {"130A": date(2026, 7, 20)}
                ),
                self._earnings_file(
                    self._earnings_url("07_0715"), date(2026, 7, 15), {"130A": date(2026, 7, 25)}
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 20))

    def test_jpx_earnings_snapshot_warns_when_the_index_layout_breaks_precedence(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        files = (
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        by_url = {source.url: source for source in files}
        with (
            patch.object(provider, "_resolve_earnings_calendar_urls", return_value=tuple(by_url)),
            patch.object(
                provider, "_download_earnings_calendar", side_effect=lambda url: by_url[url]
            ),
            self.assertLogs("baibai_engine.screening.providers.jpx", level="WARNING") as logs,
        ):
            provider.get_earnings_calendar_snapshot(date(2026, 7, 14))

        messages = "\n".join(logs.output)
        self.assertIn("no rolling `kessan` file", messages)
        self.assertIn("no readable publication stamp", messages)

    def test_jpx_earnings_snapshot_warns_when_several_rolling_files_are_listed(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        files = (
            self._earnings_file(
                self._earnings_url(), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url(), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        by_url = {source.url: source for source in files}
        with (
            patch.object(provider, "_resolve_earnings_calendar_urls", return_value=tuple(by_url)),
            patch.object(
                provider, "_download_earnings_calendar", side_effect=lambda url: by_url[url]
            ),
            self.assertLogs("baibai_engine.screening.providers.jpx", level="WARNING") as logs,
        ):
            provider.get_earnings_calendar_snapshot(date(2026, 7, 14))

        self.assertIn("several rolling `kessan` files", "\n".join(logs.output))

    def test_jpx_earnings_parser_reads_publication_stamp_but_not_cohort_titles(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        header = ["決算発表予定日", "コード"]

        stamped = provider._parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [
                    ["2027年3月期第1四半期決算（2026年8月6日開示予定分）", ""],
                    ["2026年7月30日 現在", ""],
                    header,
                    ["2026-08-07", "130A"],
                ]
            ),
            url,
        )
        self.assertEqual(stamped.published_on, date(2026, 7, 30))

        english_only = provider._parse_earnings_calendar_excel(
            self._earnings_xlsx([["As of 2026/7/2", ""], header, ["2026-08-07", "130A"]]),
            url,
        )
        self.assertEqual(english_only.published_on, date(2026, 7, 2))

        untitled = provider._parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [
                    ["６月に四半期末を迎えた決算発表予定会社の一覧", ""],
                    header,
                    ["2026-08-07", "130A"],
                ]
            ),
            url,
        )
        self.assertIsNone(untitled.published_on)

        # An impossible date in one cell must not hide a readable stamp beside it.
        beside_a_bad_cell = provider._parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [["2026年13月40日 現在", "As of 2026/7/2"], header, ["2026-08-07", "130A"]]
            ),
            url,
        )
        self.assertEqual(beside_a_bad_cell.published_on, date(2026, 7, 2))

    def test_jpx_earnings_parser_fails_on_conflict_inside_one_file(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        with self.assertRaisesRegex(JPXProviderError, "conflicting JPX earnings dates"):
            provider._parse_earnings_calendar_excel(
                self._earnings_xlsx(
                    [
                        ["決算発表予定日", "コード"],
                        ["2026-08-06", "130A"],
                        ["2026-08-07", "130A"],
                    ]
                ),
                "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/k.xlsx",
            )

    def test_jpx_earnings_index_resolves_all_allowed_cohort_links(self) -> None:
        html = b"""
        <a href="./files/kessan-1.xlsx">one</a>
        <a href="/listing/event-schedules/financial-announcement/files/kessan-2.xls">two</a>
        <a href="https://example.com/listing/event-schedules/financial-announcement/kessan-3.xlsx">external</a>
        <a href="/listing/event-schedules/other/kessan-4.xlsx">other</a>
        <a href="./files/notes.xlsx">notes</a>
        """
        provider = JPXProvider(Path("/tmp"), session=_FixedHtmlSession(html))

        self.assertEqual(
            provider._resolve_earnings_calendar_urls(),
            (
                (
                    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
                    "files/kessan-1.xlsx"
                ),
                (
                    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
                    "files/kessan-2.xls"
                ),
            ),
        )

    def test_jpx_parse_special_alert_margin_rows_extracts_marked_codes(self) -> None:
        class FakeFrame:
            def __init__(self, rows: list[list[str]]) -> None:
                self._rows = rows

            def fillna(self, value: str) -> FakeFrame:
                del value
                return self

            @property
            def iloc(self) -> FakeFrame:
                return self

            def __getitem__(self, item: slice) -> FakeFrame:
                return FakeFrame(self._rows[item])

            def iterrows(self):
                for index, row in enumerate(self._rows):
                    yield index, SimpleNamespace(tolist=lambda row=row: row)

        class FakePandas:
            @staticmethod
            def read_excel(*args, **kwargs) -> FakeFrame:
                del args, kwargs
                return FakeFrame(
                    [[""] * 7 for _ in range(7)]
                    + [
                        ["B", "○", "", "Ａｂａｌａｎｃｅ　普通株式", "スタンダード", "制", "38560"],
                        ["B", "規", "", "地域新聞社　普通株式", "スタンダード", "制", "21640"],
                        ["B", "○", "", "旅工房　普通株式", "グロース", "制", "65480"],
                    ]
                )

        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_special_alert_margin_rows(
            FakePandas(), b"", "特別注意銘柄", "https://example.com/mtdaily.xls"
        )
        self.assertEqual(
            rows,
            [
                {"code": "38560", "flag": "特別注意銘柄"},
                {"code": "65480", "flag": "特別注意銘柄"},
            ],
        )

    def test_jpx_resolves_latest_special_attention_xls_from_index(self) -> None:
        index_url = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(self._read_jpx_fixture("special_caution_index.html")),
            special_caution_index_url=index_url,
        )

        resolved = provider._resolve_special_attention_xls_url(date(2026, 4, 24))

        self.assertEqual(
            resolved,
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk2026042300.xls",
        )

    def test_jpx_resolve_special_attention_xls_rejects_non_jpx_link(self) -> None:
        html = """
        <html><body>
          <a href="https://example.com/mtdailyk2026042300.xls">Excel</a>
          <p>特別注意銘柄について信用取引残高を日々公表しています。</p>
        </body></html>
        """.encode()
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(JPXProviderError, "https://www\\.jpx\\.co\\.jp"):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_fails_when_no_candidates(self) -> None:
        html = """
        <html><body>
          <a href="/markets/statistics-equities/margin/readme.pdf">PDF</a>
          <p>特別注意銘柄について信用取引残高を日々公表しています。</p>
        </body></html>
        """.encode()
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(
            JPXProviderError, "failed to locate JPX special caution Excel link"
        ):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_raises_on_index_http_error(self) -> None:
        class FakeResponse:
            content = b""
            status_code = 503
            headers: dict[str, str] = {}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(
            Path("/tmp"),
            session=FakeSession(),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(
            JPXProviderError, "failed to download JPX special caution index"
        ):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_falls_back_to_last_link_without_date_token(
        self,
    ) -> None:
        html = """
        <html><body>
          <h2>個別銘柄信用取引残高表</h2>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk.xls">old</a>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk-latest.xls">latest</a>
        </body></html>
        """.encode()
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        self.assertEqual(
            provider._resolve_special_attention_xls_url(date(2026, 4, 24)),
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk-latest.xls",
        )

    def test_jpx_resolve_special_attention_xls_prefers_later_link_for_same_date(self) -> None:
        html = b"""
        <html><body>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk2026042300.xls">old</a>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk2026042301.xls">revised</a>
        </body></html>
        """
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        self.assertEqual(
            provider._resolve_special_attention_xls_url(date(2026, 4, 24)),
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk2026042301.xls",
        )

    def test_jpx_get_regulation_snapshot_resolves_special_attention_index_before_download(
        self,
    ) -> None:
        class CapturingProvider(JPXProvider):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.downloaded_urls: list[str] = []

            def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
                self.downloaded_urls.append(url)
                return [{"code": "38560", "flag": source_name}]

        index_url = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"
        with tempfile.TemporaryDirectory() as tmp:
            provider = CapturingProvider(
                Path(tmp),
                session=_FixedHtmlSession(self._read_jpx_fixture("special_caution_index.html")),
                special_caution_index_url=index_url,
            )
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.flags_by_ticker, {"3856": ("特別注意銘柄",)})
        self.assertEqual(
            provider.downloaded_urls,
            [
                (
                    "https://www.jpx.co.jp/markets/statistics-equities/margin/"
                    "tvdivq0000001r92-att/mtdailyk2026042300.xls"
                ),
            ],
        )

    def test_jpx_download_rows_rejects_non_jpx_origin(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get(self, url: str, timeout: int):
                del timeout
                self.calls.append(url)
                raise AssertionError("request should not be attempted")

        session = FakeSession()
        provider = JPXProvider(Path("/tmp"), session=session)

        with self.assertRaisesRegex(JPXProviderError, "https://www\\.jpx\\.co\\.jp"):
            provider._download_rows(
                "整理銘柄", "https://example.com/listing/market-alerts/supervision/"
            )

        self.assertEqual(session.calls, [])

    def test_jpx_parse_reorganization_html_rows_from_content_type(self) -> None:
        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        class FakeSession:
            def __init__(self, response: FakeResponse) -> None:
                self._response = response

            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return self._response

        provider = JPXProvider(
            Path("/tmp"),
            session=FakeSession(FakeResponse(self._read_jpx_fixture("reorganization.html"))),
        )
        rows = provider._download_rows(
            "整理銘柄", "https://www.jpx.co.jp/listing/market-alerts/supervision/"
        )

        self.assertEqual(
            rows,
            [
                {"code": "4917", "flag": "整理銘柄"},
                {"code": "7426", "flag": "整理銘柄"},
            ],
        )

    def test_jpx_parse_trading_halt_html_rows_extracts_codes(self) -> None:
        provider = JPXProvider(Path("/tmp"))

        rows = provider._parse_html_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )

        self.assertEqual(rows, [{"code": "9941", "flag": "取引停止"}])

    def test_jpx_parse_delisting_warning_html_rows_extracts_codes(self) -> None:
        provider = JPXProvider(Path("/tmp"))

        rows = provider._parse_html_rows(
            "上場廃止警告",
            self._read_jpx_fixture("delisting_warning.html"),
            "https://www.jpx.co.jp/listing/stocks/delisted/",
        )

        self.assertEqual(
            rows,
            [
                {"code": "7709", "flag": "上場廃止警告"},
                {"code": "7426", "flag": "上場廃止警告"},
            ],
        )

    def test_jpx_parse_html_rows_fail_fast_on_layout_change(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        broken_html = (
            self._read_jpx_fixture("reorganization.html")
            .decode("utf-8")
            .replace("整理銘柄", "整理銘柄一覧")
        )

        with self.assertRaisesRegex(JPXProviderError, "failed to locate JPX HTML table"):
            provider._parse_html_rows(
                "整理銘柄",
                broken_html.encode("utf-8"),
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_fails_on_invalid_html_code(self) -> None:
        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        class FakeSession:
            def __init__(self, responses: dict[str, FakeResponse]) -> None:
                self._responses = responses

            def get(self, url: str, timeout: int) -> FakeResponse:
                del timeout
                return self._responses[url]

        url = "https://www.jpx.co.jp/markets/equities/suspended/"
        broken_html = (
            self._read_jpx_fixture("trading_halt.html")
            .decode("utf-8")
            .replace("9941", "99411", 1)
            .encode("utf-8")
        )

        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={"取引停止": url},
                session=FakeSession({url: FakeResponse(broken_html)}),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_trading_halt_empty_marker_returns_no_rows(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_html_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt_empty.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )
        self.assertEqual(rows, [])

    def test_jpx_trading_halt_flat_header_fails_fast(self) -> None:
        # ヘッダが 1 段になった場合、header_rows=2 を要求しているので 2 行目 (data) が
        # `<th>` 全列ではなく fail-fast に落ちる。これにより JPX が rowspan を外して
        # 出してきても列ズレで silently 1 件欠損する事故を防げる。
        provider = JPXProvider(Path("/tmp"))
        with self.assertRaisesRegex(JPXProviderError, "unexpected JPX HTML header layout"):
            provider._parse_html_rows(
                "取引停止",
                self._read_jpx_fixture("trading_halt_flat_header.html"),
                "https://www.jpx.co.jp/markets/equities/suspended/",
            )

    def test_jpx_reorganization_nested_table_uses_outer_table_only(self) -> None:
        # 外側 table.fixedhead の data 行に nested table が入っているケース。
        # HTMLParser の stack で外側のみ index される設計が壊れたら、内側の行が
        # 外側 row として混入し header check で raise されるか、コード列に
        # 別の値が入って test 期待値と不一致になる。
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_html_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization_nested.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
        )
        self.assertEqual(rows, [{"code": "4917", "flag": "整理銘柄"}])

    def test_jpx_download_rows_rejects_unsupported_format(self) -> None:
        class FakeResponse:
            content = b"<plain>"
            status_code = 200
            headers: dict[str, str] = {"content-type": "text/plain; charset=UTF-8"}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(Path("/tmp"), session=FakeSession())
        with self.assertRaisesRegex(JPXProviderError, "unsupported JPX regulation source format"):
            provider._download_rows(
                "整理銘柄",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/notes",
            )

    def test_jpx_download_rows_raises_on_http_error_status(self) -> None:
        class FakeResponse:
            content = b""
            status_code = 503
            headers: dict[str, str] = {}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(Path("/tmp"), session=FakeSession())
        with self.assertRaisesRegex(JPXProviderError, "status=503"):
            provider._download_rows(
                "整理銘柄",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_uses_cache_without_calling_session(self) -> None:
        class ExplodingSession:
            def get(self, url: str, timeout: int):
                del url, timeout
                raise AssertionError("session.get must not be called when cache is present")

        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            sqlite_path = Path(tmp) / "data" / "screening" / "market.sqlite"
            store_jpx_regulations(
                sqlite_path,
                date(2026, 4, 24),
                flags_by_ticker={"4917": ("整理銘柄",)},
                source_names=("整理銘柄",),
            )

            provider = JPXProvider(
                cache_dir,
                regulation_urls={"整理銘柄": url},
                session=ExplodingSession(),
                sqlite_path=sqlite_path,
            )
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.source_names, ("整理銘柄",))
        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})

    def test_jpx_get_regulation_snapshot_records_fetched_at_utc_on_fetch(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b"code\n49170\n"
            headers = {"content-type": "text/csv"}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/list.csv"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            sqlite_path = Path(tmp) / "data" / "screening" / "market.sqlite"
            provider = JPXProvider(
                cache_dir,
                regulation_urls={"整理銘柄": url},
                session=FakeSession(),
                sqlite_path=sqlite_path,
            )
            provider.get_regulation_snapshot(date(2026, 4, 24))
            rows = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT fetched_at_utc FROM jpx_regulation_sources WHERE asof_date = ?",
                    ("2026-04-24",),
                )
                .fetchall()
            )

        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][0])

    def test_jpx_get_regulation_snapshot_rejects_rows_without_code_column(self) -> None:
        class FakeResponse:
            status_code = 200
            content = b"unexpected\n49170\n"
            headers = {"content-type": "text/csv"}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(
            Path("/tmp"),
            regulation_urls={
                "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/list.csv"
            },
            session=FakeSession(),
        )

        with self.assertRaisesRegex(JPXProviderError, "missing JPX code column"):
            provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_decode_html_text_prefers_content_type_charset(self) -> None:
        # JPX が cp932 ページを Content-Type で告知してきた場合、フォールバックの utf-8
        # が偶然成功して文字化けが残るリスクを避けるため、charset を最優先で試行する。
        provider = JPXProvider(Path("/tmp"))
        cp932_payload = "整理銘柄".encode("cp932")
        decoded = provider._decode_html_text(
            cp932_payload,
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=Shift_JIS",
        )
        self.assertEqual(decoded, "整理銘柄")

    def test_jpx_decode_html_text_raises_when_all_encodings_fail(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        # 0xFF は cp932 / utf-8 / utf-8-sig いずれでも lead byte として無効なので、
        # フォールバック 3 段すべて UnicodeDecodeError で落ちる。LookupError 経路の
        # 確認も兼ねて、Content-Type には未知の encoding 名を載せる。
        with self.assertRaises(JPXProviderError):
            provider._decode_html_text(
                b"\x81\x00\x81\x00",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
                "text/html; charset=jpx-unknown",
            )

    def test_jpx_reorganization_invalid_code_raises(self) -> None:
        broken_html = (
            self._read_jpx_fixture("reorganization.html")
            .decode("utf-8")
            .replace("4917", "49171", 1)
            .encode("utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={
                    "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/"
                },
                session=_FixedHtmlSession(broken_html),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_delisting_warning_invalid_code_raises(self) -> None:
        broken_html = (
            self._read_jpx_fixture("delisting_warning.html")
            .decode("utf-8")
            .replace("7709", "77091", 1)
            .encode("utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={"上場廃止警告": "https://www.jpx.co.jp/listing/stocks/delisted/"},
                session=_FixedHtmlSession(broken_html),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))
