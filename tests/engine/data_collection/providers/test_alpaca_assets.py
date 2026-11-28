from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from httpx2 import ByteStream, ConnectError, MockTransport, Response

from baibai_engine.data_collection.providers.alpaca_assets import (
    ASSETS_BODY_MAX_BYTES,
    AlpacaAssetsReceipt,
    parse_alpaca_us_assets,
)
from baibai_engine.data_collection.providers.alpaca_http import (
    AlpacaCredentials,
    receive_alpaca,
    receive_paper_us_assets,
)

_URL = "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
_AT = datetime(2026, 11, 30, 8, 30, tzinfo=timezone(timedelta(hours=-5)))
_ID = "901237e3-3b76-47ec-b432-046db621571b"
_OTHER_ID = "0cad6215-b3e6-4d2e-888a-ec704bd5e33c"
_KEY = AlpacaCredentials("paper-test", "paper-secret-test")


def _asset(**changes):
    return {
        "id": _ID,
        "class": "us_equity",
        "exchange": "NASDAQ",
        "symbol": "AAPL",
        "name": "Apple Inc. Common Stock",
        "status": "active",
        "tradable": True,
        "attributes": ["has_options", "future_attribute"],
        **changes,
    }


def _native(*rows):
    return json.dumps(rows, ensure_ascii=False).encode()


def test_decodes_exact_native_fields_without_classifying_product_or_dropping_nontradable():
    original = _native(
        _asset(id=_ID.upper(), securityType="not authoritative"),
        _asset(
            id=_OTHER_ID,
            exchange="NEW-VENUE",
            symbol="CLASS-A",
            name="Example Société",
            attributes=None,
            tradable=False,
        ),
    )
    first, second = parse_alpaca_us_assets(original)
    assert (first.id, first.exchange, first.symbol, first.name, first.status) == (
        UUID(_ID),
        "NASDAQ",
        "AAPL",
        "Apple Inc. Common Stock",
        "active",
    )
    assert first.tradable is True
    assert first.attributes == ("has_options", "future_attribute")
    assert not hasattr(first, "product_type")
    assert (second.id, second.exchange, second.symbol, second.name) == (
        UUID(_OTHER_ID),
        "NEW-VENUE",
        "CLASS-A",
        "Example Société",
    )
    assert second.tradable is False
    assert second.attributes == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"class": "crypto"},
        {"class": None},
        {"status": "inactive"},
        {"id": "no-uuid"},
        {"id": _ID.replace("-", "")},
        {"exchange": ""},
        {"exchange": "\tNYSE"},
        {"symbol": ""},
        {"symbol": "\nAAPL"},
        {"name": "  "},
        {"name": 123},
        {"name": "Company\nShares"},
        {"tradable": 1},
        {"tradable": "false"},
        {"attributes": ["has_options", False]},
    ],
)
def test_bad_row_invalidates_the_full_universe(changes):
    with pytest.raises(ValueError, match="Alpaca Assets"):
        parse_alpaca_us_assets(_native(_asset(id=_OTHER_ID), _asset(**changes)))


@pytest.mark.parametrize("present", [False, True], ids=["absent", "null"])
def test_missing_or_null_name_is_neither_required_nor_invented(present):
    row = _asset(name=None)
    if not present:
        del row["name"]
    body = _native(row)
    receipt = AlpacaAssetsReceipt(_AT, body)
    assert receipt.assets[0].name is None
    assert receipt.assets[0].symbol == "AAPL"
    assert receipt.body == body


def test_name_preserves_legitimate_unicode_spacing():
    name = "Müller\u00a0Holdings\u202fSociété"
    (asset,) = parse_alpaca_us_assets(_native(_asset(name=name)))
    assert asset.name == name


@pytest.mark.parametrize("field", ["id", "class", "status", "symbol", "exchange", "tradable"])
def test_provider_identity_scope_and_tradability_cannot_be_missing(field):
    row = _asset()
    del row[field]
    with pytest.raises(ValueError, match="invalid active US equity row"):
        parse_alpaca_us_assets(_native(row))


def test_duplicate_uuid_even_under_a_different_symbol_or_case_is_rejected():
    with pytest.raises(ValueError, match="repeats an asset UUID"):
        parse_alpaca_us_assets(_native(_asset(), _asset(id=_ID.upper(), symbol="ANOTHER")))


@pytest.mark.parametrize(
    "body", [b"", b"[]", b"null", b"{}", b"[", b"[null]", b'[{"status":1,"status":1}]']
)
def test_envelope_is_a_complete_nonempty_unambiguous_array(body):
    with pytest.raises(ValueError, match="Alpaca Assets"):
        parse_alpaca_us_assets(body)


def test_fixed_paper_assets_get_records_original_bytes_clock_and_hash():
    original = b"   " + _native(_asset()) + b"\n"
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        assert request.headers["APCA-API-KEY-ID"] == _KEY.key
        assert request.headers["APCA-API-SECRET-KEY"] == _KEY.secret
        assert request.headers["Accept-Encoding"] == "identity"
        return Response(
            200,
            request=request,
            stream=ByteStream(original),
            headers={"Content-Type": "application/json", "Content-Length": str(len(original))},
        )

    received = receive_paper_us_assets(
        credentials=_KEY, transport=MockTransport(respond), clock=lambda: _AT
    )
    assert requests == [("GET", _URL)]
    assert received.body == original
    assert received.body_sha256 == sha256(original).hexdigest()
    assert received.received_at == datetime(2026, 11, 30, 13, 30, tzinfo=UTC)
    assert received.received_at.tzinfo is UTC
    assert len(received.assets) == 1
    with pytest.raises(FrozenInstanceError):
        received.assets[0].name = "another name"
    with pytest.raises(FrozenInstanceError):
        received.body = b"[]"
    assert _KEY.secret not in repr(received)
    with pytest.raises(ValueError, match="timezone-aware"):
        AlpacaAssetsReceipt(_AT.replace(tzinfo=None), original)


@pytest.mark.parametrize("status", [206, 302, 401, 403, 429, 503])
def test_non_success_is_not_an_empty_result_and_is_not_retried_or_redirected(status):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return Response(status, request=request, headers={"Location": _URL})

    with pytest.raises(OSError, match=f"Alpaca Assets HTTP {status}"):
        receive_paper_us_assets(
            credentials=_KEY, transport=MockTransport(respond), clock=lambda: _AT
        )
    assert calls == [_URL]


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        (_URL.replace("status=active", "status=all"), {}),
        (_URL.replace("paper-api", "api"), {}),
        (_URL, {"Content-Range": "bytes 0-8/100"}),
        (_URL, {"Content-Length": "1"}),
    ],
)
def test_wrong_endpoint_or_partial_http_response_cannot_produce_a_receipt(url, headers):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return Response(200, request=request, headers=headers, stream=ByteStream(_native(_asset())))

    with pytest.raises(OSError, match="Alpaca"):
        receive_alpaca(
            url,
            endpoint="assets",
            credentials=_KEY,
            transport=MockTransport(respond),
            clock=lambda: _AT,
        )
    assert calls == ([_URL] if url == _URL else [])


def test_transport_failure_and_body_limit_are_reported_without_payload_or_retries():
    def fail(request):
        raise ConnectError("fixture transport detail", request=request)

    with pytest.raises(OSError, match=r"^Alpaca response could not be fully read$"):
        receive_paper_us_assets(credentials=_KEY, transport=MockTransport(fail), clock=lambda: _AT)

    oversized = b" " * (ASSETS_BODY_MAX_BYTES + 1)

    def respond(request):
        return Response(200, request=request, stream=ByteStream(oversized))

    with pytest.raises(OSError, match="exceeds its byte bound"):
        receive_paper_us_assets(
            credentials=_KEY, transport=MockTransport(respond), clock=lambda: _AT
        )
    with pytest.raises(ValueError, match="bounded original response"):
        parse_alpaca_us_assets(oversized)
