from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest
import requests
from requests.structures import CaseInsensitiveDict
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.transport.context import (
    IndicatorsProviderError,
    ProviderFetchStoppedError,
)
from baibai_jobs.integration import us_listing_directory_loader
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    read_current_listing_master,
)
from baibai_jobs.integration.us_listing_directory_loader import (
    load_verified_nasdaq_directory_inputs,
)
from baibai_jobs.integration.us_listing_refresh import refresh_current_us_listings

_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
_DAYS_SESSION = date(2026, 11, 27)
_STARTED = datetime(2026, 11, 27, 13, 43, tzinfo=UTC)
_FIRST_RECEIPT = _STARTED + timedelta(seconds=12, microseconds=12)
_SECOND_RECEIPT = _STARTED + timedelta(seconds=44, microseconds=27)

_NASDAQ_BODY = (
    b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    b"WMT|Synthetic Nasdaq common stock|Q|N|N|100|N|N\n"
    b"File Creation Time: 1127202608:42|||||||\n"
)
_OTHER_BODY = (
    b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
    b"BRK.B|Synthetic NYSE Class B common stock|N|BRK.B|N|100|N|BRKB\n"
    b"File Creation Time: 1127202608:40|||||||\n"
)


class _Response(requests.Response):
    def __init__(self, url, body, *, status=200, headers=None, error=None):
        super().__init__()
        self.url = url
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.fixture_body = body
        self.error = error
        self.body_started = False
        self.closed = False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        assert chunk_size > 1
        assert decode_unicode is False
        self.body_started = True
        midpoint = len(self.fixture_body) // 2
        yield self.fixture_body[:midpoint]
        if self.error is not None:
            raise self.error
        yield self.fixture_body[midpoint:]

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        assert kwargs["params"] is None
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == 30
        assert kwargs["headers"] == {"Accept-Encoding": "identity"}
        return next(self.responses)


class _Store(MemoryR2Store):
    def get_checked_object(self, key, *, max_bytes):
        response = self.get_bytes_with_metadata(key)
        if response is not None:
            assert len(response[0]) <= max_bytes
        return response


def _clock(*moments):
    return iter(moments or (_STARTED, _FIRST_RECEIPT, _SECOND_RECEIPT)).__next__


def _load(session, *, verify_scope, clock=None, target=_DAYS_SESSION):
    return load_verified_nasdaq_directory_inputs(
        session, target, now=_clock() if clock is None else clock, verify_scope=verify_scope
    )


def _not_attested(*_):
    pytest.fail("invalid acquisition must not reach session/scope attestation")


def test_direct_two_file_receipts_expose_exact_bytes_and_independent_times():
    first = _Response(_NASDAQ_URL, _NASDAQ_BODY, headers={"content-length": str(len(_NASDAQ_BODY))})
    second = _Response(_OTHER_URL, _OTHER_BODY, headers={"transfer-encoding": "chunked"})
    http = _Session(first, second)
    observed = []

    def verify_scope(target, pair):
        assert target == _DAYS_SESSION
        assert first.closed
        assert second.closed
        observed.extend(pair)
        return True

    result = _load(
        http,
        verify_scope=verify_scope,
        clock=_clock(
            _STARTED, _FIRST_RECEIPT, _SECOND_RECEIPT.astimezone(ZoneInfo("America/New_York"))
        ),
    )
    assert http.gets == [_NASDAQ_URL, _OTHER_URL]
    assert [item.source_url for item in observed] == http.gets
    assert [item.body for item in observed] == [_NASDAQ_BODY, _OTHER_BODY]
    assert [item.creation_stamp for item in observed] == ["1127202608:42", "1127202608:40"]
    assert [item.byte_count for item in observed] == [len(_NASDAQ_BODY), len(_OTHER_BODY)]
    assert [item.body_sha256 for item in observed] == [
        sha256(body).hexdigest() for body in (_NASDAQ_BODY, _OTHER_BODY)
    ]
    assert [r.received_at for r in result.receipts] == [_FIRST_RECEIPT, _SECOND_RECEIPT]
    assert all(r.received_at.tzinfo is UTC and r.complete is True for r in result.receipts)
    assert [r.source_session for r in result.receipts] == [_DAYS_SESSION, _DAYS_SESSION]
    assert [r.body for r in result.receipts] == [_NASDAQ_BODY, _OTHER_BODY]
    assert result.verified_events == ()


@pytest.mark.parametrize("status", [201, 204, 206, 302, 403, 429, 503])
def test_only_direct_whole_http_200_without_fallback_or_second_request(status):
    response = _Response(_NASDAQ_URL, _NASDAQ_BODY, status=status)
    http = _Session(response)
    with pytest.raises(ProviderFetchStoppedError, match=f"HTTP 200.*got {status}"):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL]
    assert response.closed
    assert not response.body_started


@pytest.mark.parametrize(
    ("headers", "reason", "body_started"),
    [
        ({"content-range": "bytes 0-9/20"}, "not direct and complete", False),
        ({"content-encoding": "gzip"}, "not identity-encoded", False),
        ({"content-length": "junk"}, "invalid .*Content-Length", False),
        ({"content-length": "0"}, "invalid .*Content-Length", False),
        ({"content-length": "9000000"}, "invalid .*Content-Length", False),
        ({"content-length": str(len(_NASDAQ_BODY) + 1)}, "size does not match", True),
        ({"content-length": str(len(_NASDAQ_BODY) - 1)}, "size does not match", True),
    ],
)
def test_incomplete_or_ambiguous_http_body_is_never_attested(headers, reason, body_started):
    first = _Response(_NASDAQ_URL, _NASDAQ_BODY, headers=headers)
    http = _Session(first)
    with pytest.raises(ProviderFetchStoppedError, match=reason):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL]
    assert first.closed
    assert first.body_started is body_started


def test_final_url_must_be_the_exact_requested_public_url():
    response = _Response(_OTHER_URL, _NASDAQ_BODY)
    http = _Session(response)
    with pytest.raises(ProviderFetchStoppedError, match="not direct and complete"):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL]
    assert response.closed
    assert not response.body_started


def test_unknown_length_still_has_a_streaming_limit(monkeypatch):
    monkeypatch.setattr(
        us_listing_directory_loader, "MAX_CSV_RESPONSE_BYTES", len(_NASDAQ_BODY) - 1
    )
    response = _Response(_NASDAQ_URL, _NASDAQ_BODY)
    http = _Session(response)
    with pytest.raises(ProviderFetchStoppedError, match="response exceeds the byte limit"):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL]
    assert response.closed


def test_interrupted_stream_is_not_a_complete_receipt():
    response = _Response(_NASDAQ_URL, _NASDAQ_BODY, error=requests.Timeout("fixture"))
    http = _Session(response)
    with pytest.raises(IndicatorsProviderError, match="failed to receive Nasdaq directory"):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL]
    assert response.closed


@pytest.mark.parametrize(
    ("bad_body", "reason"),
    [
        (b"", "response size does not match"),
        (_OTHER_BODY.replace(b"11272026", b"11252026"), "native creation date"),
        (_OTHER_BODY + b"trailing bytes\n", "file creation record"),
        (_OTHER_BODY[: _OTHER_BODY.index(b"File Creation Time")], "symbol directory header"),
    ],
)
def test_both_provider_terminal_footers_must_be_present_and_current(bad_body, reason):
    first = _Response(_NASDAQ_URL, _NASDAQ_BODY)
    second = _Response(_OTHER_URL, bad_body)
    http = _Session(first, second)
    with pytest.raises((ProviderFetchStoppedError, ValueError), match=reason):
        _load(http, verify_scope=_not_attested)
    assert http.gets == [_NASDAQ_URL, _OTHER_URL]
    assert first.closed
    assert second.closed


@pytest.mark.parametrize(
    ("moments", "reason", "expected_requests"),
    [
        ((_STARTED.replace(tzinfo=None),), "current New York receipt date", 0),
        ((_STARTED - timedelta(days=1),), "current New York receipt date", 0),
        (
            (_STARTED, _FIRST_RECEIPT, datetime(2026, 11, 28, 5, tzinfo=UTC)),
            "current New York receipt date",
            2,
        ),
        ((_STARTED, _SECOND_RECEIPT, _FIRST_RECEIPT), "clock moved backwards", 2),
    ],
)
def test_per_file_clock_uses_actual_local_date_and_does_not_claim_rolled_over_pair(
    moments, reason, expected_requests
):
    http = _Session(_Response(_NASDAQ_URL, _NASDAQ_BODY), _Response(_OTHER_URL, _OTHER_BODY))
    with pytest.raises(ValueError, match=reason):
        _load(http, verify_scope=_not_attested, clock=_clock(*moments))
    assert len(http.gets) == expected_requests


def _refresh(store, http, *, is_open, verify_scope):
    return refresh_current_us_listings(
        store,
        created_at=_STARTED,
        now=lambda: _SECOND_RECEIPT + timedelta(seconds=7),
        scheduled=True,
        is_open=lambda day: is_open if day == _DAYS_SESSION else None,
        load_verified=lambda target: _load(http, verify_scope=verify_scope, target=target),
    )


@pytest.mark.parametrize("verification", [None, False, True])
def test_composition_requires_independent_provider_attestation_before_writing(verification):
    store = _Store()
    http = _Session(_Response(_NASDAQ_URL, _NASDAQ_BODY), _Response(_OTHER_URL, _OTHER_BODY))

    def verify_scope(*_):
        return verification

    if verification is not True:
        with pytest.raises(ValueError, match="session and completeness are unverified"):
            _refresh(store, http, is_open=True, verify_scope=verify_scope)
        assert not store.get_keys
        assert not store.put_keys
    else:
        assert _refresh(store, http, is_open=True, verify_scope=verify_scope) == "updated"
        us = read_current_listing_master(store)["US"]
        assert {listing.key for listing in us.slice.rows} == {("XNAS", "WMT"), ("XNYS", "BRK.B")}
        assert us.received_at == _SECOND_RECEIPT
        assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]
        assert not store.delete_keys
    assert http.gets == [_NASDAQ_URL, _OTHER_URL]


@pytest.mark.parametrize("is_open", [False, None])
def test_composition_checks_verified_calendar_before_touching_http(is_open):
    store = _Store()
    http = _Session()
    if is_open is False:
        assert _refresh(store, http, is_open=is_open, verify_scope=_not_attested) == "closed"
    else:
        with pytest.raises(ValueError, match="verified US market calendar"):
            _refresh(store, http, is_open=is_open, verify_scope=_not_attested)
    assert not http.gets
    assert not store.get_keys
    assert not store.put_keys
