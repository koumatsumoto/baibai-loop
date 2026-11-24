"""Synthetic tests of the Alpaca wire boundary; nothing contacts the provider."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from baibai_engine.data_collection.providers.alpaca import (
    BARS_URL,
    MAX_PAGES,
    PAGE_LIMIT,
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)

QUERY = DailyBarsQuery(
    ("META", "BRK.B"), date(2025, 11, 25), date(2025, 11, 29), date(2025, 11, 29), "raw"
)
AT = datetime(2026, 11, 23, 10, 15, tzinfo=UTC)


def _page(groups=None, next_token=None, *, when=AT):
    return BarsResponse(
        200,
        when,
        json.dumps(
            {"bars": {} if groups is None else groups, "next_page_token": next_token}
        ).encode(),
    )


def _capture(*responses, query=QUERY, request_id="synthetic-1", max_pages=MAX_PAGES):
    calls = []
    pending = iter(responses)

    def transport(url):
        calls.append(url)
        response = next(pending)
        if isinstance(response, OSError):
            raise response
        return response

    result = capture_daily_bars(
        query, request_id=request_id, transport=transport, max_pages=max_pages
    )
    return result, calls


def test_request_encodes_only_the_pinned_daily_scope():
    url = QUERY.url()
    path, params = url.split("?", 1)
    assert path == BARS_URL
    assert "symbols=META%2CBRK.B" in params
    assert parse_qs(params) == {
        "symbols": ["META,BRK.B"],
        "timeframe": ["1Day"],
        "start": ["2025-11-25"],
        "end": ["2025-11-29"],
        "limit": [str(PAGE_LIMIT)],
        "adjustment": ["raw"],
        "asof": ["2025-11-29"],
        "feed": ["iex"],
        "currency": ["USD"],
        "sort": ["asc"],
    }
    different = replace(QUERY, adjustment="split", symbol_asof="-")
    split = parse_qs(urlsplit(different.url()).query)
    assert split["adjustment"] == ["split"]
    assert split["asof"] == ["-"]


def test_short_or_empty_pages_follow_the_opaque_cursor_and_keep_native_symbol_labels():
    first = BarsResponse(
        200,
        AT,
        b' { "bars": {"FB":[{"t":"provider-time","v":true}]},'
        b' "extra": "kept exactly", "next_page_token": "A+b/c=?&literal" }\n',
    )
    second = _page(next_token="another")
    last = _page({"BRK.B": [{"t": "another-original-row"}]})
    result, calls = _capture(first, second, last)
    assert result.complete
    assert result.failure is None
    assert result.query.symbols == ("META", "BRK.B")
    assert [receipt.returned_symbols for receipt in result.pages] == [("FB",), (), ("BRK.B",)]
    assert [receipt.response.body for receipt in result.pages] == [
        first.body,
        second.body,
        last.body,
    ]
    assert [receipt.page_number for receipt in result.pages] == [1, 2, 3]
    assert calls[0] == QUERY.url()
    for url, token in zip(calls[1:], ["A+b/c=?&literal", "another"], strict=True):
        target = urlsplit(url)
        assert target.netloc == "data.alpaca.markets"
        assert target.path == "/v2/stocks/bars"
        params = parse_qs(target.query)
        assert params.pop("page_token") == [token]
        assert params == parse_qs(urlsplit(calls[0]).query)
    assert "A%2Bb%2Fc%3D%3F%26literal" in calls[1]
    # No report/repr includes private native bodies or opaque continuation values.
    assert "kept exactly" not in repr(result)
    assert "literal" not in repr(result)


def test_disabled_mapping_refuses_an_unrequested_symbol_without_following_more_pages():
    no_mapping = replace(QUERY, symbol_asof="-")
    first = _page({"META": [{}]}, next_token="first-token")
    wrong = _page({"FB": [{}]}, next_token="do-not-follow")
    result, calls = _capture(first, wrong, query=no_mapping)
    assert not result.complete
    assert result.failure == "scope_mismatch"
    assert [page.returned_symbols for page in result.pages] == [("META",), ("FB",)]
    assert result.pages[-1].response.body == wrong.body
    assert len(calls) == 2


def test_disabled_mapping_can_complete_without_a_row_for_every_requested_symbol():
    result, _ = _capture(_page({"META": [{}]}), query=replace(QUERY, symbol_asof="-"))
    assert result.complete
    assert result.query.symbols == ("META", "BRK.B")
    assert result.pages[0].returned_symbols == ("META",)


def test_empty_final_page_is_wire_complete_not_proof_of_a_market_day():
    result, calls = _capture(_page(), max_pages=1)
    assert result.complete
    assert result.pages[0].returned_symbols == ()
    assert calls == [QUERY.url()]


def test_equal_body_and_receipt_identity_are_distinct():
    response = _page()
    first = _capture(response)[0].pages[0]
    replay = _capture(response)[0].pages[0]
    new_attempt = _capture(response, request_id="synthetic-2")[0].pages[0]
    later = _capture(replace(response, received_at=AT + timedelta(seconds=1)))[0].pages[0]
    wire_change = _capture(replace(response, body=response.body + b"\n"))[0].pages[0]
    same_instant = AT.astimezone(timezone(timedelta(hours=-5)))
    equivalent_clock = _capture(replace(response, received_at=same_instant))[0].pages[0]
    assert first.receipt_id == replay.receipt_id == equivalent_clock.receipt_id
    assert equivalent_clock.response.received_at == AT
    assert first.body_sha256 == new_attempt.body_sha256 == later.body_sha256
    assert first.body_sha256 == hashlib.sha256(response.body).hexdigest()
    assert (
        len({first.receipt_id, new_attempt.receipt_id, later.receipt_id, wire_change.receipt_id})
        == 4
    )
    assert wire_change.body_sha256 != first.body_sha256
    disabled_mapping = _capture(response, query=replace(QUERY, symbol_asof="-"))[0].pages[0]
    assert disabled_mapping.body_sha256 == first.body_sha256
    assert disabled_mapping.receipt_id != first.receipt_id


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_non_http_200_is_retained_without_retry_or_redirect(status):
    first = _page(next_token="next")
    refused = BarsResponse(status, AT, b'{"message":"fixture, not log text"}')
    result, calls = _capture(first, refused)
    assert not result.complete
    assert result.failure == "http_status"
    assert [page.response for page in result.pages] == [first, refused]
    assert result.pages[1].returned_symbols is None
    assert len(calls) == 2
    assert "not log text" not in repr(result)


def test_transport_failure_keeps_prior_page_without_fabricating_a_receipt():
    first = _page(next_token="next")
    result, calls = _capture(first, OSError("arbitrary transport text"))
    assert not result.complete
    assert result.failure == "transport_error"
    assert [page.response for page in result.pages] == [first]
    assert len(calls) == 2
    assert "arbitrary transport text" not in repr(result)


def test_backward_sequential_receipt_keeps_offending_wire_and_stops():
    first = _page({"META": [{}]}, "next", when=AT + timedelta(seconds=1))
    backward = _page({"BRK.B": [{}]}, "do-not-follow", when=AT)
    result, calls = _capture(first, backward, _page())

    assert not result.complete
    assert result.failure == "receipt_time_order"
    assert [receipt.response for receipt in result.pages] == [first, backward]
    assert [receipt.returned_symbols for receipt in result.pages] == [("META",), ("BRK.B",)]
    assert result.pages[-1].body_sha256 == hashlib.sha256(backward.body).hexdigest()
    assert len(calls) == 2


def test_same_sequential_receipt_time_is_permitted_at_native_precision():
    first = _page({"META": [{}]}, "next", when=AT)
    second = _page({"BRK.B": [{}]}, when=AT)
    result, calls = _capture(first, second)
    assert result.complete
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        (b"not JSON", "invalid_envelope"),
        (b"\xff", "invalid_envelope"),
        (b'{"bars":null,"next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{"AAPL":[]},"bars":{"OTHER":[]},"next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{},"next_page_token":"continue","next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{"AAPL":{}},"next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{"AAPL":[123]},"next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{"AAPL":[{"c":NaN}]},"next_page_token":null}', "invalid_envelope"),
        (b'{"bars":{}}', "invalid_cursor"),
        (b'{"bars":{},"next_page_token":""}', "invalid_cursor"),
        (b'{"bars":{},"next_page_token":false}', "invalid_cursor"),
        (b'{"bars":{},"next_page_token":"\\ud800"}', "invalid_cursor"),
    ],
)
def test_wire_failure_keeps_native_response_and_cannot_act_as_end_of_chain(body, failure):
    result, calls = _capture(BarsResponse(200, AT, body))
    assert not result.complete
    assert result.failure == failure
    assert result.pages[0].response.body == body
    assert calls == [QUERY.url()]


def test_page_row_limit_is_for_all_symbols():
    page = _page({"META": [{}] * PAGE_LIMIT, "BRK.B": [{}]})
    result, calls = _capture(page)
    assert result.failure == "invalid_envelope"
    assert not result.complete
    assert result.pages[0].response.body == page.body
    assert len(calls) == 1


def test_cycle_and_page_bound_do_not_issue_extra_requests():
    first = _page(next_token="cursor-a")
    second = _page(next_token="cursor-b")
    repeated = _page(next_token="cursor-a")
    cycled, calls = _capture(first, second, repeated)
    assert not cycled.complete
    assert cycled.failure == "pagination_cycle"
    assert len(calls) == 3
    assert cycled.pages[-1].response == repeated

    bounded, calls = _capture(first, second, max_pages=2)
    assert not bounded.complete
    assert bounded.failure == "page_limit"
    assert len(calls) == 2
    assert len(bounded.pages) == 2
    completed, calls = _capture(first, _page(), max_pages=2)
    assert completed.complete
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"symbols": ()},
        {"symbols": "XYZ"},
        {"symbols": (True,)},
        {"symbols": ("AAPL", "AAPL")},
        {"symbols": ("brk.b",)},
        {"symbols": ("AAPL,MSFT",)},
        {"start": datetime(2025, 11, 25, tzinfo=UTC)},
        {"end": date(2025, 11, 22)},
        {"symbol_asof": None},
        {"adjustment": "all"},
    ],
)
def test_invalid_request_fields_are_not_coerced(change):
    with pytest.raises(ValueError, match=r"daily bars|select a fixed symbol date"):
        replace(QUERY, **change)


@pytest.mark.parametrize("bound", [True, 0, MAX_PAGES + 1])
def test_invalid_request_bound_fails_before_transport(bound):
    with pytest.raises(ValueError, match="page bound"):
        _capture(max_pages=bound)


def test_no_implicit_clock_or_bytes_coercion():
    with pytest.raises(ValueError, match="time"):
        BarsResponse(200, AT.replace(tzinfo=None), b"{}")
    with pytest.raises(ValueError, match="bytes"):
        BarsResponse(200, AT, bytearray())
    with pytest.raises(ValueError, match="status"):
        BarsResponse(True, AT, b"{}")
