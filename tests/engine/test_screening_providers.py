from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from itertools import count, permutations
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import requests
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.http_doubles import (
    ExplodingSession,
    FakeResponse,
    FakeSession,
    json_response,
    response,
    sequence,
)

ROOT = Path(__file__).resolve().parents[2]

from baibai_engine.collection.edinet import http as edinet_http
from baibai_engine.collection.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.market import edinet_refresh as edinet_refresh_module
from baibai_engine.market.edinet_decode import normalize_metric_record, parse_sec_code
from baibai_engine.market.edinet_document_events import parse_doc_id
from baibai_engine.market.edinet_metrics.csv import parse_csv_zip_metric_record
from baibai_engine.market.edinet_metrics.revision import EDINET_METRIC_REVISION
from baibai_engine.market.edinet_metrics.write import store_edinet_metrics
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.market.edinet_refresh import EDINETRefresh
from baibai_engine.market.edinet_selection import select_document_candidates
from baibai_engine.market.financial_documents import (
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.market.jpx_decode import (
    _decode_html_text,
    _EarningsCalendarFile,
    _parse_special_attention_html_rows,
    build_earnings_calendar_snapshot,
    build_regulation_snapshot,
    parse_earnings_calendar_excel,
    parse_regulation_rows,
)
from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
)
from baibai_engine.market.jquants_decode import (
    JQuantsProviderError,
    normalize_adjustment_factor_event,
    normalize_daily_bar,
    normalize_market_calendar,
    parse_jquants_code,
)
from baibai_engine.market.metric_quality import TTMQuality


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


def _edinet_csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    csv_text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        f"{element}\t{context}\tJPY\t{value}" for element, context, value in rows
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
            expected = _edinet_csv_zip([("jpcrp_cor:NetSales", "CurrentYearDuration", "1000")])
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
            expected = _edinet_csv_zip([("jpcrp_cor:NetSales", "CurrentYearDuration", "1000")])
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

            with patch("baibai_engine.collection.edinet.http.time.sleep"):
                content = provider.download_csv_zip("S100TEST")

            self.assertEqual(content, expected)
            self.assertEqual(
                EDINETReader(cache / "market.sqlite").read_csv_zip("S100TEST"), expected
            )
            self.assertFalse((cache / "edinet/csv_zips/S100TEST.zip").exists())

    def test_download_csv_zip_retries_transient_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = _edinet_csv_zip([("jpcrp_cor:NetSales", "CurrentYearDuration", "1000")])
            session = _TransientThenBytesSession(content=expected)
            provider = EDINETRefresh(
                "key", sqlite_path=Path(tmp) / "market.sqlite", session=session
            )

            with patch("baibai_engine.collection.edinet.http.time.sleep") as sleep:
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

            with patch("baibai_engine.collection.edinet.http.time.sleep") as sleep:
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
                patch("baibai_engine.collection.edinet.http.time.sleep") as sleep,
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
                patch("baibai_engine.collection.edinet.http.time.sleep"),
                self.assertRaisesRegex(EdinetRateLimitError, "rate limited"),
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
            ("jpcrp_cor:NetSales", "CurrentYearDuration", "1000"),
            (
                "jppfs_cor:NetCashProvidedByUsedInOperatingActivities",
                "CurrentYearDuration",
                "150",
            ),
            ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "90"),
            ("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "30"),
            (
                "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
                "CurrentYearDuration",
                "-40",
            ),
            ("jppfs_cor:PurchaseOfIntangibleAssetsInvCF", "CurrentYearDuration", "0"),
            ("jpcrp_cor:CashAndDeposits", "CurrentYearInstant", "300"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "250"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "20"),
            ("jppfs_cor:LongTermLoansPayable", "CurrentYearInstant", "50"),
            ("jppfs_cor:NetAssets", "CurrentYearInstant", "800"),
            ("jppfs_cor:Assets", "CurrentYearInstant", "1400"),
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
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "250"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant_NonConsolidatedMember", "900"),
            (
                "jppfs_cor:SharesOfSubsidiariesAndAssociates",
                "CurrentYearInstant",
                "700",
            ),
            (
                "jpcrp_cor:OperationalInvestmentSecurities",
                "CurrentYearInstant",
                "800",
            ),
            (
                "jppfs_cor:GainOnSaleOfInvestmentSecurities",
                "CurrentYearDuration",
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
        self.assertNotIn("metric_unavailable:investment_securities", record.failure_reasons)

    def test_investment_securities_excludes_similar_tags_and_accepts_reported_zero(self) -> None:
        excluded = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(
                [
                    (
                        "jpcrp_cor:OperationalInvestmentSecurities",
                        "CurrentYearInstant",
                        "800",
                    ),
                    (
                        "jppfs_cor:ProceedsFromSaleOfInvestmentSecurities",
                        "CurrentYearDuration",
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
                [("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "-")]
            ),
        )

        self.assertIsNone(excluded.investment_securities)
        self.assertIn("metric_unavailable:investment_securities", excluded.failure_reasons)
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
                                "CurrentYearInstant",
                                value,
                            )
                        ]
                    ),
                )

    def test_parse_csv_zip_metric_record_extracts_loan_payable_and_lease_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "120"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "30"),
            ("jppfs_cor:LeaseAssetsPPE", "CurrentYearInstant", "999"),
            ("jppfs_cor:LongTermLoansReceivable", "CurrentYearInstant", "888"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.debt, 150.0)
        self.assertEqual(record.net_cash, 850.0)
        self.assertNotIn("metric_unavailable:debt", record.failure_reasons)

    def test_parse_csv_zip_metric_record_keeps_missing_debt_unavailable(
        self,
    ) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
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
        self.assertIn("metric_unavailable:debt", record.failure_reasons)

    def test_parse_csv_zip_metric_record_allows_reported_zero_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "-"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "-"),
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
        self.assertNotIn("metric_unavailable:debt", record.failure_reasons)

    def test_edinet_reader_reads_cached_metrics_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "data" / "screening" / "market.sqlite"
            document = {
                "seqNumber": 1,
                "docID": "S100SAVED",
                "secCode": "72030",
                "docTypeCode": "120",
                "csvFlag": "1",
                "xbrlFlag": "1",
                "legalStatus": "1",
                "disclosureStatus": "0",
                "withdrawalStatus": "0",
                "docInfoEditStatus": "0",
                "periodStart": "2025-04-01",
                "periodEnd": "2026-03-31",
                "submitDateTime": "2026-05-01 12:00",
            }
            store_edinet_documents(sqlite_path, date(2026, 5, 1), [document])
            candidate = select_document_candidates([document]).candidates["7203"]
            store_edinet_metrics(
                sqlite_path,
                date(2026, 5, 1),
                [
                    {
                        "ticker": "7203",
                        "cash": 100.0,
                        "debt": 10.0,
                        "source_doc_id": candidate.doc_id,
                        "document_type": candidate.doc_type_code,
                        "source_submit_datetime": candidate.submit_datetime,
                        "source_period_start": candidate.period_start,
                        "source_period_end": candidate.period_end,
                        "extractor_revision": EDINET_METRIC_REVISION,
                        "source_document_revision": candidate.source_document_revision,
                    }
                ],
            )

            reader = EDINETReader(sqlite_path)
            records = reader.load_metric_records(date(2026, 5, 1))

        self.assertEqual(records["7203"].cash, 100.0)
        self.assertEqual(records["7203"].debt, 10.0)

    def test_edinet_refresh_requires_api_key_for_unretained_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = EDINETRefresh(None, sqlite_path=Path(tmpdir) / "market.sqlite")
            with self.assertRaisesRegex(EdinetAcquisitionError, "EDINET_API_KEY"):
                provider.download_csv_zip("S100TEST")

    def test_normalize_market_calendar_business_day(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_reads_native_timestamp(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24T00:00:00", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_treats_half_day_as_business(self) -> None:
        # HolDiv="2" は半日営業 (大納会など取引あり) で、business_day 扱いすべき。
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
                "DiscNo": "synthetic-88910-2026-04-01",
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

    def test_normalize_daily_bar_reads_native_fields(self) -> None:
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

    def test_normalize_daily_bar_keeps_raw_close_despite_provider_adjustment(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 200.0,
                "AdjC": 100.0,
                "AdjFactor": 0.5,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 200.0)
        self.assertEqual(bar.adjustment_factor, 0.5)

    def test_adjusted_only_record_does_not_invent_raw_close_or_lose_split_event(self) -> None:
        record = {
            "Code": "67310",
            "Date": "2023-12-27",
            "C": None,
            "AdjC": 500.0,
            "AdjFactor": 100.0,
        }
        self.assertIsNone(normalize_daily_bar(record))
        event = normalize_adjustment_factor_event(record)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.adjustment_factor, 100.0)

    def test_reported_action_without_factor_is_not_silently_discarded(self) -> None:
        for kind in ("1", "2", "3", "unknown-future-kind"):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(
                    JQuantsProviderError, "corporate action has no usable adjustment factor"
                ),
            ):
                normalize_adjustment_factor_event(
                    {
                        "Code": "13010",
                        "Date": "2026-10-01",
                        "C": None,
                        "AdjFactor": None,
                        "ExRT": kind,
                    }
                )
        self.assertIsNone(
            normalize_adjustment_factor_event(
                {"Code": "13010", "Date": "2026-10-01", "C": None, "AdjFactor": None}
            )
        )

    def test_normalize_adjustment_event_does_not_require_close(self) -> None:
        event = normalize_adjustment_factor_event(
            {
                "Code": "67310",
                "Date": "2023-12-27T00:00:00",
                "C": None,
                "AdjFactor": 100.0,
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.ticker, "6731")
        self.assertEqual(event.adjustment_factor, 100.0)

    def test_normalize_financial_summary_preserves_native_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-88910-2026-04-01",
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
        self.assertIsNone(summary.forecast_eps)
        self.assertIn('"FEPS": "377.75"', summary.document_payload or "")
        self.assertEqual(summary.eps_ttm, 300.50)
        self.assertEqual(summary.shares_outstanding, 1000000.0)
        self.assertEqual(summary.operating_profit, 1840000000.0)
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_projection_selects_next_fiscal_year_by_dates(self) -> None:
        fy = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": "",
                "NxFEPS": "360.26",
                "EPS": "349.18",
            }
        )
        assert fy is not None
        self.assertIsNone(fy.forecast_eps)
        fy = project_financial_documents([fy], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(fy.forecast_eps, 360.26)
        self.assertEqual(fy.eps_ttm, 349.18)

    def test_non_finite_current_forecasts_do_not_cross_into_next_period(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
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
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
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
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
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
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)

    def test_projection_keeps_zero_current_forecasts(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
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
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(summary.forecast_eps, 0.0)
        self.assertEqual(summary.forecast_profit, 1_000_000_000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 900_000_000.0)
        self.assertEqual(summary.dps_forecast_annual, 0.0)

    def test_normalize_financial_summary_keeps_average_shares_separate(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-41670-2024-08-14",
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

    def test_projection_selects_current_fiscal_year_by_dates(self) -> None:
        q3 = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-02-13",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
                "Code": "97150",
                "DiscDate": "2026-02-13T00:00:00",
                "FEPS": "300.0",
                "NxFEPS": "145.0",
            }
        )
        assert q3 is not None
        self.assertIsNone(q3.forecast_eps)
        q3 = project_financial_documents([q3], date.fromisoformat("2026-02-13"))[0]
        self.assertEqual(q3.forecast_eps, 300.0)

    def test_projection_keeps_current_forecast_profit_pair(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-48490-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
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
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(summary.forecast_profit, 5464000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 3406000000.0)

    def test_projection_keeps_next_forecast_profit_pair(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-48490-2026-04-01",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
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
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-04-01"))[0]
        self.assertEqual(summary.forecast_eps, 120.0)
        self.assertEqual(summary.forecast_profit, 2000000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 2500000000.0)

    def test_normalize_financial_summary_preserves_fiscal_period_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-88910-2026-04-01",
                "Code": "88910",
                "DiscDate": "2026-04-01",
                "CurPerType": "1Q",
                "CurFYEn": "2027-03-31",
                "CurPerSt": "2026-04-01",
                "CurPerEn": "2026-06-30",
                "EPS": "300.50",
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
                "DiscNo": "synthetic-25935-2026-04-24",
                "Code": "25935",
                "DiscDate": "2026-04-24T00:00:00",
                "EPS": "10.0",
            }
        )
        self.assertIsNone(summary)

    def test_jpx_earnings_parser_detects_header_and_normalizes_rows(self) -> None:
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

        parsed = parse_earnings_calendar_excel(
            content,
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx",
        )

        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in parsed.entries],
            [("130A", date(2026, 7, 15))],
        )
        self.assertEqual(parsed.raw_record_count, 3)
        self.assertEqual(parsed.excluded_record_count, 2)
        self.assertEqual(parsed.undated_tickers, ("7203",))
        self.assertIsNone(parsed.published_on)

    def test_jpx_earnings_parser_fails_on_layout_and_invalid_code(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        with self.assertRaisesRegex(JPXProviderError, "header layout"):
            parse_earnings_calendar_excel(
                self._earnings_xlsx([["Date", "Code"], ["2026-07-15", "130A"]]), url
            )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
            parse_earnings_calendar_excel(
                self._earnings_xlsx([["決算発表日", "コード"], ["2026-07-15", "INVALID"]]),
                url,
            )

    def test_jpx_earnings_snapshot_fails_when_every_date_is_past(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/a/kessan.xlsx"
        with self.assertRaisesRegex(JPXProviderError, "only past dates"):
            build_earnings_calendar_snapshot(
                (self._earnings_file(url, None, {"130A": date(2026, 7, 13)}),),
                asof_date=date(2026, 7, 14),
                source_urls=(url,),
            )

    def _merge_earnings_files(
        self, files: tuple[_EarningsCalendarFile, ...], asof_date: date
    ) -> JPXEarningsCalendarSnapshot:
        by_url = {source.url: source for source in files}
        snapshots = [
            build_earnings_calendar_snapshot(
                tuple(by_url[url] for url in urls), asof_date=asof_date, source_urls=urls
            )
            for urls in permutations(by_url)
        ]
        canonical = replace(snapshots[0], source_urls=tuple(sorted(snapshots[0].source_urls)))
        for snapshot in snapshots[1:]:
            self.assertEqual(
                canonical, replace(snapshot, source_urls=tuple(sorted(snapshot.source_urls)))
            )
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

    def test_jpx_earnings_snapshot_keeps_equal_priority_conflicts_unknown(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(
                    self._earnings_url("06_0715"), date(2026, 7, 15), {"130A": date(2026, 7, 20)}
                ),
                self._earnings_file(
                    self._earnings_url("07_0715"),
                    date(2026, 7, 15),
                    {"130A": date(2026, 7, 25), "7203": date(2026, 7, 26)},
                ),
            ),
            date(2026, 7, 14),
        )
        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in snapshot.entries],
            [("7203", date(2026, 7, 26))],
        )

    def test_jpx_earnings_snapshot_warns_when_archive_publication_stamp_is_missing(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        with self.assertLogs("baibai_engine.market.jpx_decode", level="WARNING") as logs:
            build_earnings_calendar_snapshot(
                files, asof_date=date(2026, 7, 14), source_urls=tuple(f.url for f in files)
            )
        self.assertIn("no readable publication stamp", "\n".join(logs.output))

    def test_jpx_earnings_snapshot_without_daily_file_is_not_a_layout_warning(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url("09_1009"), date(2026, 10, 8), {"130A": date(2026, 10, 20)}
            ),
        )
        with self.assertNoLogs("baibai_engine.market.jpx_decode", level="WARNING"):
            snapshot = self._merge_earnings_files(files, date(2026, 10, 15))
        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 10, 20))

    def test_jpx_earnings_snapshot_warns_when_several_rolling_files_are_listed(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url(), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url(), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        with self.assertLogs("baibai_engine.market.jpx_decode", level="WARNING") as logs:
            build_earnings_calendar_snapshot(
                files, asof_date=date(2026, 7, 14), source_urls=tuple(f.url for f in files)
            )
        self.assertIn("several rolling `kessan` files", "\n".join(logs.output))

    def test_jpx_earnings_parser_reads_publication_stamp_but_not_cohort_titles(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        header = ["決算発表予定日", "コード"]

        stamped = parse_earnings_calendar_excel(
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

        english_only = parse_earnings_calendar_excel(
            self._earnings_xlsx([["As of 2026/7/2", ""], header, ["2026-08-07", "130A"]]),
            url,
        )
        self.assertEqual(english_only.published_on, date(2026, 7, 2))

        untitled = parse_earnings_calendar_excel(
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
        beside_a_bad_cell = parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [["2026年13月40日 現在", "As of 2026/7/2"], header, ["2026-08-07", "130A"]]
            ),
            url,
        )
        self.assertEqual(beside_a_bad_cell.published_on, date(2026, 7, 2))

    def test_jpx_earnings_parser_drops_a_ticker_dated_twice_and_keeps_the_file(self) -> None:
        """2026-08-05: one issuer with two dates stopped the whole fetch, and the day.

        The ticker leaves as undated — the same exit a 未定 row takes — and every
        other issuer in the file is still delivered. A cell that is not a date at
        all takes the same exit.
        """

        with self.assertLogs("baibai_engine.market.jpx_decode", level="WARNING") as logs:
            parsed = parse_earnings_calendar_excel(
                self._earnings_xlsx(
                    [
                        ["決算発表予定日", "コード"],
                        ["2026-08-06", "130A"],
                        ["2026-08-07", "130A"],
                        ["2026-08-07", "1301"],
                        ["2026-08-08", "130A"],
                        ["8月上旬", "1305"],
                    ]
                ),
                "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/k.xlsx",
            )

        self.assertEqual(
            parsed.entries,
            (JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 8, 7)),),
        )
        self.assertEqual(parsed.raw_record_count, 5)
        self.assertEqual(parsed.excluded_record_count, 4)
        self.assertIn("conflicting=1 unparsable=1", logs.output[0])
        self.assertIn("1305: '8月上旬'", logs.output[0])
        self.assertIn("130A: 2026-08-06 and 2026-08-07 and 2026-08-08", logs.output[0])

    def test_jpx_special_attention_current_sections(self) -> None:
        html = self._read_jpx_fixture("special_caution.html")
        snapshot = build_regulation_snapshot(
            {
                "特別注意銘柄": parse_regulation_rows(
                    "特別注意銘柄",
                    html,
                    "https://www.jpx.co.jp/listing/measures/alert/index.html",
                )
            }
        )
        self.assertEqual(
            snapshot.flags_by_ticker, {"130A": ("特別注意銘柄",), "2901": ("特別注意銘柄",)}
        )
        self.assertEqual(snapshot.source_names, ("特別注意銘柄",))

    def test_jpx_special_attention_merges_and_deduplicates_sections(self) -> None:
        html = self._read_jpx_fixture("special_caution.html").decode()
        html = html.replace(
            "<p>現在、該当する情報はありません。</p>",
            "<table><tr><th>銘柄名</th><th>コード</th></tr>"
            "<tr><td>A</td><td>29010</td></tr><tr><td>C</td><td>6548</td></tr></table>",
        )
        rows = _parse_special_attention_html_rows(html, "fixture")
        self.assertEqual(
            rows,
            [{"code": code, "flag": "特別注意銘柄"} for code in ("2901", "130A", "6548")],
        )

    def test_jpx_special_attention_all_sections_empty(self) -> None:
        html = "<h2>現在の指定状況</h2><p>現在、該当する情報はありません。</p>"
        self.assertEqual(_parse_special_attention_html_rows(html, "fixture"), [])

    def test_jpx_special_attention_rejects_layout_drift(self) -> None:
        html = self._read_jpx_fixture("special_caution.html").decode()
        cases = {
            "missing code header": html.replace("コード", "識別子"),
            "short code row": html.replace('<td rowspan="2">2901</td>', ""),
            "missing continuation": html.replace("<tr><td>-</td><td>-</td></tr>", "", 1),
            "broken continuation": html.replace(
                "<tr><td>-</td><td>-</td></tr>", "<tr><td>-</td></tr>"
            ),
            "changed rowspan": html.replace('rowspan="2">2901', 'rowspan="3">2901'),
            "changed colspan": html.replace('rowspan="2">2901', 'rowspan="2" colspan="2">2901'),
            "invalid code": html.replace(">2901<", ">invalid<"),
            "missing section content": html.replace("<p>現在、該当する情報はありません。</p>", ""),
            "unrelated table": "<h2>指定履歴</h2><table><tr><th>コード</th></tr><tr><td>9999</td></tr></table>",
            "unrelated empty marker": "<h2>現在の指定状況</h2><h2>指定履歴</h2><p>現在、該当する情報はありません。</p>",
            "header only": "<h2>現在の指定状況</h2><table><tr><th>コード</th></tr></table>",
        }
        for label, changed in cases.items():
            with self.subTest(label=label), self.assertRaises(JPXProviderError):
                _parse_special_attention_html_rows(changed, "fixture")

    def test_jpx_parse_reorganization_html_rows_from_content_type(self) -> None:
        rows = parse_regulation_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=UTF-8",
        )
        self.assertEqual(
            rows,
            [
                {"code": "4917", "flag": "整理銘柄"},
                {"code": "7426", "flag": "整理銘柄"},
            ],
        )

    def test_jpx_parse_trading_halt_html_rows_extracts_codes(self) -> None:

        rows = parse_regulation_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )

        self.assertEqual(rows, [{"code": "9941", "flag": "取引停止"}])

    def test_jpx_parse_delisting_warning_html_rows_extracts_codes(self) -> None:

        rows = parse_regulation_rows(
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
        broken_html = (
            self._read_jpx_fixture("reorganization.html")
            .decode("utf-8")
            .replace("整理銘柄", "整理銘柄一覧")
        )

        with self.assertRaisesRegex(JPXProviderError, "failed to locate JPX HTML table"):
            parse_regulation_rows(
                "整理銘柄",
                broken_html.encode("utf-8"),
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_fails_on_invalid_html_code(self) -> None:
        broken_html = (
            self._read_jpx_fixture("trading_halt.html")
            .decode("utf-8")
            .replace("9941", "99411", 1)
            .encode("utf-8")
        )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
            build_regulation_snapshot(
                {
                    "取引停止": parse_regulation_rows(
                        "取引停止", broken_html, "https://www.jpx.co.jp/markets/equities/suspended/"
                    )
                }
            )

    def test_jpx_trading_halt_empty_marker_returns_no_rows(self) -> None:
        rows = parse_regulation_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt_empty.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )
        self.assertEqual(rows, [])

    def test_jpx_trading_halt_flat_header_fails_fast(self) -> None:
        # ヘッダが 1 段になった場合、header_rows=2 を要求しているので 2 行目 (data) が
        # `<th>` 全列ではなく fail-fast に落ちる。これにより JPX が rowspan を外して
        # 出してきても列ズレで silently 1 件欠損する事故を防げる。
        with self.assertRaisesRegex(JPXProviderError, "unexpected JPX HTML header layout"):
            parse_regulation_rows(
                "取引停止",
                self._read_jpx_fixture("trading_halt_flat_header.html"),
                "https://www.jpx.co.jp/markets/equities/suspended/",
            )

    def test_jpx_reorganization_nested_table_uses_outer_table_only(self) -> None:
        # 外側 table.fixedhead の data 行に nested table が入っているケース。
        # HTMLParser の stack で外側のみ index される設計が壊れたら、内側の行が
        # 外側 row として混入し header check で raise されるか、コード列に
        # 別の値が入って test 期待値と不一致になる。
        rows = parse_regulation_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization_nested.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
        )
        self.assertEqual(rows, [{"code": "4917", "flag": "整理銘柄"}])

    def test_jpx_decode_html_text_prefers_content_type_charset(self) -> None:
        # JPX が cp932 ページを Content-Type で告知してきた場合、フォールバックの utf-8
        # が偶然成功して文字化けが残るリスクを避けるため、charset を最優先で試行する。
        cp932_payload = "整理銘柄".encode("cp932")
        decoded = _decode_html_text(
            cp932_payload,
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=Shift_JIS",
        )
        self.assertEqual(decoded, "整理銘柄")

    def test_jpx_decode_html_text_raises_when_all_encodings_fail(self) -> None:
        # 0x81 に続く 0x00 は cp932 の有効なバイト列ではなく、utf-8 / utf-8-sig
        # でも decode できない。LookupError 経路の
        # 確認も兼ねて、Content-Type には未知の encoding 名を載せる。
        with self.assertRaises(JPXProviderError):
            _decode_html_text(
                b"\x81\x00\x81\x00",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
                "text/html; charset=jpx-unknown",
            )

    def test_jpx_refuses_an_invalid_code_in_every_regulation_page(self) -> None:
        cases = (
            (
                "整理銘柄",
                "reorganization.html",
                b"4917",
                b"49171",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            ),
            (
                "上場廃止警告",
                "delisting_warning.html",
                b"7709",
                b"77091",
                "https://www.jpx.co.jp/listing/stocks/delisted/",
            ),
        )
        for name, fixture, code, broken, url in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(JPXProviderError, "invalid JPX code"),
            ):
                build_regulation_snapshot(
                    {
                        name: parse_regulation_rows(
                            name, self._read_jpx_fixture(fixture).replace(code, broken, 1), url
                        )
                    }
                )


def test_capex_original_edinet_rows():
    # EDINET API v2 documents/{doc_id}?type=5、2026-09-17取得の原本CSV行を抜粋。
    # S100YU8C InterimDurationは2026-01-01..06-30 (会社半期報告書id=500)。
    cases = [
        ("5946", "S100YU8C", "160", 4_857_000_000, 1_136_000_000, 3_721_000_000, "approximated"),
        ("7122", "S100YI50", "120", 15_630_000_000, 1_831_000_000, 13_799_000_000, "approximated"),
        ("8127", "S100XXXU", "160", 871_844_000, 100_902_000, 770_942_000, "approximated"),
    ]
    for ticker, doc, kind, ocf, capex, fcf, quality in cases:
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "XBRL_TO_CSV/statement.csv",
                (ROOT / "tests/fixtures/edinet" / f"{doc}.tsv").read_text().encode("utf-16"),
            )
        record = parse_csv_zip_metric_record(
            ticker=ticker,
            doc_id=doc,
            doc_type_code=kind,
            content=content.getvalue(),
            period_end=date(2026, 12, 31) if ticker == "5946" else None,
        )
        assert (record.ocf_ttm, record.capex_ttm, record.fcf_ttm) == (ocf, capex, fcf)
        assert record.ttm_quality_fcf == quality
        assert record.capex_source == "purchase_of_fixed_assets"
        assert "metric_unavailable:capex" not in record.failure_reasons


def test_annual_cash_flow_details_from_original_edinet_rows():
    cases = (
        (
            "2415",
            "S100YIDK",
            "120",
            (-54_148_000, 31_636_000, -12_897_000, 21_080_000, None, 241_674_000),
            (741_370_000, 263_164_000),
            (3_282_582_000, 1_004_534_000, 2_278_048_000),
        ),
        (
            "4231",
            "S100YK4M",
            "120",
            (-654_271_000, -459_700_000, -409_250_000, None, None, None),
            (None, None),
            (2_416_413_000, 3_577_928_000, -1_161_515_000),
        ),
        (
            "1814",
            "S100YK21",
            "120",
            (1_239_000_000, None, 4_078_000_000, None, None, 1_021_000_000),
            (157_000_000, 95_000_000),
            (9_299_000_000, 252_000_000, 9_047_000_000),
        ),
        (
            "7991",
            "S100YPE4",
            "130",
            (975_027_000, 119_225_000, -1_351_721_000, None, None, None),
            (3_415_260_000, 43_566_000),
            (-715_467_000, 3_458_826_000, -4_174_293_000),
        ),
        (
            "7122",
            "S100YI50",
            "120",
            (223_000_000, -4_537_000_000, 1_802_000_000, 14_489_000_000, None, None),
            (1_679_000_000, 152_000_000),
            (15_630_000_000, 1_831_000_000, 13_799_000_000),
        ),
    )
    for ticker, doc, kind, effects, capex_parts, totals in cases:
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "XBRL_TO_CSV/statement.csv",
                (ROOT / "tests" / "fixtures" / "edinet" / f"{doc}.tsv")
                .read_text()
                .encode("utf-16"),
            )
        record = parse_csv_zip_metric_record(
            ticker=ticker,
            doc_id=doc,
            doc_type_code=kind,
            content=content.getvalue(),
        )
        assert (
            record.ocf_receivables_cash_effect,
            record.ocf_inventories_cash_effect,
            record.ocf_payables_cash_effect,
            record.ocf_contract_liabilities_cash_effect,
            record.ocf_advances_received_cash_effect,
            record.ocf_other_payables_cash_effect,
        ) == effects
        assert (record.capex_ppe_reported, record.capex_intangible_reported) == capex_parts
        assert (record.ocf_ttm, record.capex_ttm, record.fcf_ttm) == totals


def test_cash_flow_details_require_annual_jpy_unique_current_context():
    csv_text = "\n".join(
        (
            "要素ID\tコンテキストID\t連結・個別\tユニットID\t値",
            "jppfs_cor:NetCashProvidedByUsedInOperatingActivities\tCurrentYearDuration\t連結\tJPY\t100",
            "jppfs_cor:DecreaseIncreaseInNotesAndAccountsReceivableTradeOpeCF\tCurrentYearDuration\t連結\tJPY\t-",
            "jppfs_cor:DecreaseIncreaseInInventoriesOpeCF\tCurrentYearDuration\t連結\tJPY\t10",
            "jppfs_cor:DecreaseIncreaseInInventoriesOpeCF\tCurrentYearDuration\t連結\tJPY\t11",
            "jppfs_cor:IncreaseDecreaseInNotesAndAccountsPayableTradeOpeCF\tCurrentYearDuration\t連結\tUSD\t12",
            "jppfs_cor:IncreaseDecreaseInContractLiabilitiesOpeCF\tPrior1YearDuration\t連結\tJPY\t13",
        )
    )
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("XBRL_TO_CSV/statement.csv", csv_text.encode("utf-16"))
    annual = parse_csv_zip_metric_record(
        ticker="9999", doc_id="S100TEST", doc_type_code="120", content=content.getvalue()
    )
    assert annual.ocf_receivables_cash_effect == 0
    assert annual.ocf_inventories_cash_effect is None
    assert annual.ocf_payables_cash_effect is None
    assert annual.ocf_contract_liabilities_cash_effect is None

    interim = parse_csv_zip_metric_record(
        ticker="9999", doc_id="S100TEST", doc_type_code="160", content=content.getvalue()
    )
    assert interim.ocf_receivables_cash_effect is None
    assert interim.capex_ppe_reported is None


def test_capex_total_components_and_period_are_not_mixed(subtests):
    total = "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF"
    tangible = "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF"
    intangible = "jppfs_cor:PurchaseOfIntangibleAssetsInvCF"
    current = "InterimDuration"
    prior = "Prior1InterimDuration"
    rows = [("jppfs_cor:NetCashProvidedByUsedInOperatingActivities", current, "4857")]
    cases = [
        ("total", [(total, current, "-1136")], 1136),
        ("components", [(tangible, current, "-1000"), (intangible, current, "-136")], 1136),
        (
            "total-and-components",
            [
                (total, current, "-1136"),
                (tangible, current, "-1000"),
                (intangible, current, "-136"),
            ],
            1136,
        ),
        (
            "blank-total-does-not-fall-back",
            [(total, current, ""), (tangible, current, "-1000"), (intangible, current, "-136")],
            None,
        ),
        (
            "conflicting-total-does-not-fall-back",
            [
                (total, current, "-1136"),
                (total, current, "-2000"),
                (tangible, current, "-1000"),
                (intangible, current, "-136"),
            ],
            None,
        ),
        (
            "explicit-zero-total-precedes-components",
            [(total, current, "-"), (tangible, current, "-1000"), (intangible, current, "-136")],
            0,
        ),
        ("missing-intangible", [(tangible, current, "-1000")], None),
        ("missing-tangible", [(intangible, current, "-136")], None),
        ("prior-component", [(tangible, current, "-1000"), (intangible, prior, "-136")], None),
        (
            "prior-total",
            [(total, prior, "-769"), (tangible, current, "-1000"), (intangible, current, "-136")],
            1136,
        ),
        ("nonconsolidated-total", [(total, "InterimDuration_NonConsolidatedMember", "-999")], None),
        ("missing", [], None),
        ("explicit-zero", [(total, current, "0")], 0),
    ]
    for name, extra, expected in cases:
        with subtests.test(name=name):
            record = parse_csv_zip_metric_record(
                ticker="9999",
                doc_id="S100TEST",
                doc_type_code="160",
                content=_edinet_csv_zip(
                    rows
                    + extra
                    + [("jppfs_cor:PurchaseOfInvestmentSecuritiesInvCF", current, "-9999")]
                ),
            )
            assert record.capex_ttm == expected
            assert record.fcf_ttm == (None if expected is None else 4857 - expected)
            assert ("metric_unavailable:capex" in record.failure_reasons) == (expected is None)
            assert "metric_unavailable:cash" in record.failure_reasons


def test_missing_edinet_quality_is_unknown():
    record = normalize_metric_record(
        {"ticker": "5946", "ebitda_ttm": 1328000000, "fcf_ttm": 3721000000}
    )
    assert record.ttm_quality_ev_ebitda is None
    assert record.ttm_quality_fcf is None
