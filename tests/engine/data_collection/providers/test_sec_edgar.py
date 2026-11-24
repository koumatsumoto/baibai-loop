from __future__ import annotations

import gc
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import httpx2
import pytest
from httpx2 import ByteStream, MockTransport, Request, Response, SyncByteStream

from baibai_engine.data_collection.providers import sec_edgar
from baibai_engine.data_collection.providers.sec_edgar import (
    SecEdgarQuery,
    SecEdgarReceipt,
    capture_sec_edgar,
)

_CONTACT = "edgar-team@research.example.org"
_TIME = datetime.fromisoformat("2026-02-17T12:00:52.112233-05:00")
_ATTEMPT = UUID("f8a16217-47bc-4675-9e45-1fc2b1327df5")
_FACTS = b'{"cik":10789,"entityName":"Synthetic","facts":{}}\n'
_SUBMISSIONS = (
    b'{"cik":"0000010789","filings":{"recent":{"form":["10-Q"],'
    b'"accessionNumber":["0000099999-25-000001"],"acceptanceDateTime":["2025-12-08T20:11:12Z"]},'
    b'"files":[{"name":"CIK0000010789-submissions-001.json"}]}}'
)


def _response(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    request: Request | None = None,
) -> Response:
    return Response(status, headers=headers, stream=ByteStream(body), request=request)


def _capture(query: SecEdgarQuery, fake, **overrides):
    return capture_sec_edgar(
        query,
        **{
            "transport": MockTransport(fake),
            "contact_email": _CONTACT,
            "attempt_id": _ATTEMPT,
            "clock": lambda: _TIME,
            **overrides,
        },
    )


@pytest.mark.parametrize(
    ("endpoint", "path", "body", "has_older"),
    [
        ("companyfacts", "/api/xbrl/companyfacts", _FACTS, None),
        ("submissions", "/submissions", _SUBMISSIONS, True),
    ],
)
def test_fixed_get_only_and_original_bytes(endpoint, path, body, has_older) -> None:
    seen = []

    def fake(req):
        seen.append(req)
        return _response(body, headers={"Date": "Tue, 02 Dec 2025 04:15:05 GMT"})

    query = SecEdgarQuery(endpoint, "10789")
    receipt = _capture(query, fake)
    assert len(seen) == 1  # No older filing URL, even when advertised.
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url) == f"https://data.sec.gov{path}/CIK0000010789.json" == query.url
    assert req.headers["Host"] == "data.sec.gov"
    assert req.headers["Accept"] == "application/json"
    assert req.headers["Accept-Encoding"] == "identity"
    assert req.headers["User-Agent"] == f"Baibai Loop {_CONTACT}"
    assert "Authorization" not in req.headers
    assert "Cookie" not in req.headers
    assert isinstance(receipt, SecEdgarReceipt)
    assert receipt.state == "received"
    assert (receipt.request, receipt.attempt_id) == (query, _ATTEMPT)
    assert receipt.body == body
    assert receipt.body_sha256 == sha256(body).hexdigest()
    assert receipt.received_at == _TIME.astimezone(UTC)
    assert receipt.received_at.tzinfo is UTC
    assert receipt.older_submissions_advertised is has_older
    assert _CONTACT not in repr(receipt)
    assert body.decode() not in repr(receipt)


def test_request_and_delivery_identity_are_independent_of_operator_contact() -> None:
    q = SecEdgarQuery("companyfacts", "10789")
    same = SecEdgarQuery("companyfacts", "0000010789")
    different = SecEdgarQuery("submissions", "10789")
    assert q == same
    assert q.request_id == same.request_id != different.request_id
    assert q.request_id != SecEdgarQuery("companyfacts", "10790").request_id

    res = _capture(q, lambda _: _response(_FACTS))
    replay = _capture(
        same, lambda _: _response(_FACTS), contact_email="other-team@research.example.org"
    )
    other_attempt = _capture(
        q, lambda _: _response(_FACTS), attempt_id=UUID("40501cb9-c03d-4883-85a2-79be8a453cee")
    )
    other_body = _capture(q, lambda _: _response(_FACTS + b" "))
    other_time = _capture(
        q, lambda _: _response(_FACTS), clock=lambda: _TIME + timedelta(microseconds=1)
    )
    assert all(
        isinstance(r, SecEdgarReceipt) for r in (res, replay, other_attempt, other_body, other_time)
    )
    assert res == replay
    assert res.receipt_id.startswith("sec-edgar:")
    assert (
        len(
            {res.receipt_id, other_attempt.receipt_id, other_body.receipt_id, other_time.receipt_id}
        )
        == 4
    )
    assert res.body_sha256 == other_attempt.body_sha256 == other_time.body_sha256
    assert res.body_sha256 != other_body.body_sha256
    assert _CONTACT not in repr((res, q))
    assert _CONTACT not in res.receipt_id


@pytest.mark.parametrize(
    "cik", ["0", "0000000000", "12345678901", "１０７８９", "10789?x=1", " 10789", 10789, True]
)
def test_no_malformed_endpoint_identity(cik) -> None:
    with pytest.raises(ValueError, match="CIK"):
        SecEdgarQuery("companyfacts", cik)


def test_no_unsupported_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        SecEdgarQuery("https://data.sec.gov/files/not_allowed", "10789")


@pytest.mark.parametrize(
    "contact",
    [
        "",
        "no-mail",
        "user@example.org\r\nAuthorization: private",
        "a@example.org b@example.org",
        "тест@example.org",
    ],
)
def test_bad_contact_never_sent_or_logged(contact) -> None:
    def no_call(req):
        pytest.fail("contact must be validated before the request")

    with pytest.raises(ValueError, match="contact email") as bad:
        _capture(SecEdgarQuery("companyfacts", "10789"), no_call, contact_email=contact)
    assert "Authorization" not in str(bad.value)


@pytest.mark.parametrize(
    "location",
    [
        "https://unrelated.example.org/receive",
        "/other/CIK0000010789.json",
        "https://example.org:NaN",
    ],
)
def test_redirect_is_retained_but_never_followed(location) -> None:
    seen = []

    def fake(req):
        seen.append(req)
        return _response(b"location redirect", status=302, headers={"Location": location})

    outcome = _capture(SecEdgarQuery("companyfacts", "10789"), fake)
    assert len(seen) == 1
    assert seen[0].url.host == "data.sec.gov"
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "http_status"
    assert outcome.status_code == 302
    assert outcome.body == b"location redirect"
    assert _CONTACT not in repr(outcome)


def test_a_response_with_different_request_provenance_cannot_supply_the_filing_payload() -> None:
    requested = SecEdgarQuery("companyfacts", "10789")
    outcome = _capture(
        requested,
        lambda req: _response(
            _FACTS, request=Request("GET", "https://data.sec.gov/submissions/CIK0000010789.json")
        ),
    )
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "unexpected_response_scope"
    assert outcome.body == _FACTS


@pytest.mark.parametrize(
    ("status", "headers", "state"),
    [
        (206, {}, "partial_content"),
        (200, {"Content-Range": "bytes 0-6/41"}, "partial_content"),
        (200, {"Content-Length": "999"}, "partial_content"),
        (200, {"Content-Length": "not a length"}, "invalid_response"),
        (200, {"Content-Encoding": "gzip"}, "unsupported_content_encoding"),
        (304, {}, "http_status"),
        (403, {}, "http_status"),
        (404, {}, "http_status"),
        (429, {"Retry-After": "600"}, "http_status"),
        (503, {}, "http_status"),
    ],
)
def test_transport_failure_has_exact_body_but_cannot_become_normal_absence(
    status, headers, state
) -> None:
    seen = []

    def fake(req):
        seen.append(req)
        return _response(_FACTS, status=status, headers=headers)

    outcome = _capture(SecEdgarQuery("companyfacts", "10789"), fake)
    assert len(seen) == 1  # No retry, downgrade, or substitute endpoint.
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == state
    assert outcome.body == _FACTS
    assert outcome.body_sha256 == sha256(_FACTS).hexdigest()


class _BrokenWire(SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield _FACTS
        raise httpx2.ReadError("EXCEPTION-PROVIDER-CONTACT-MUST-NOT-LEAK")


def test_stream_break_even_after_valid_json_is_not_a_complete_receipt() -> None:
    outcome = _capture(
        SecEdgarQuery("companyfacts", "10789"),
        lambda _: Response(200, stream=_BrokenWire()),
    )
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "incomplete_transfer"
    assert outcome.body == _FACTS
    assert "EXCEPTION-PROVIDER" not in repr(outcome)
    assert outcome.received_at == _TIME


def test_no_receipt_or_clock_is_invented_if_no_response_arrives() -> None:
    def fail(_):
        raise httpx2.ConnectError("PRIVATE_NETWORK_DIAGNOSTIC")

    def clock():
        pytest.fail("a failed transport without headers cannot stamp a receipt")

    assert _capture(SecEdgarQuery("companyfacts", "10789"), fail, clock=clock) == "transport_error"


def test_limit_is_an_explicit_truncation_not_a_success(monkeypatch) -> None:
    monkeypatch.setattr(sec_edgar, "_MAX_BODY_BYTES", len(_FACTS))
    wanted = SecEdgarQuery("companyfacts", "10789")
    just_at_cap = _capture(wanted, lambda _: _response(_FACTS))
    beyond_cap = _capture(wanted, lambda _: _response(_FACTS + b" "))
    assert isinstance(just_at_cap, SecEdgarReceipt)
    assert just_at_cap.state == "received"
    assert isinstance(beyond_cap, SecEdgarReceipt)
    assert beyond_cap.state == "body_limit"
    assert beyond_cap.body == _FACTS
    assert beyond_cap.receipt_id != just_at_cap.receipt_id


@pytest.mark.parametrize(
    ("body", "state"),
    [
        (b"", "invalid_envelope"),
        (b"<html>SEC access denied</html>", "invalid_envelope"),
        (b'{"cik":10789,"facts":', "invalid_envelope"),
        (b'{"cik":10789,"facts":[]}', "invalid_envelope"),
        (b'{"cik":true,"facts":{}}', "invalid_envelope"),
        (b'{"cik":10789.0,"facts":{}}', "invalid_envelope"),
        (b'{"cik":10789,"facts":{"us-gaap":NaN}}', "invalid_envelope"),
        (b'{"cik":1,"cik":10789,"facts":{}}', "invalid_envelope"),
        (b'{"cik":10790,"facts":{}}', "issuer_mismatch"),
        (_SUBMISSIONS, "invalid_envelope"),
    ],
)
def test_malformed_or_foreign_cik_is_retained_as_evidence(body, state) -> None:
    receipt = _capture(SecEdgarQuery("companyfacts", "10789"), lambda _: _response(body))
    assert isinstance(receipt, SecEdgarReceipt)
    assert receipt.state == state
    assert receipt.body == body


def test_submissions_checks_compact_column_counts_but_not_acceptance_as_a_clock() -> None:
    q = SecEdgarQuery("submissions", "10789")
    native = json.loads(_SUBMISSIONS)
    native["filings"]["recent"]["filingDate"] = []
    wrong_count = json.dumps(native).encode()
    outcome = _capture(q, lambda _: _response(wrong_count))
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "invalid_envelope"
    assert outcome.body == wrong_count

    native["filings"]["recent"]["filingDate"].append("not interpreted by wire capture")
    native["filings"]["files"].clear()
    still_wire_only = _capture(q, lambda _: _response(json.dumps(native).encode()))
    assert isinstance(still_wire_only, SecEdgarReceipt)
    assert still_wire_only.state == "received"
    assert still_wire_only.older_submissions_advertised is False
    assert still_wire_only.received_at == _TIME


def test_query_subclass_cannot_choose_a_destination_for_the_contact() -> None:
    class _ForeignUrl(SecEdgarQuery):
        @property
        def url(self) -> str:
            return "https://unrelated.example.org/receive"

    def no_call(req):
        pytest.fail("only the validated concrete SEC request may declare a contact")

    with pytest.raises(ValueError, match="validated SEC query"):
        _capture(_ForeignUrl("companyfacts", "10789"), no_call)


def test_conflicting_http_framing_does_not_turn_into_a_valid_response() -> None:
    outcome = _capture(
        SecEdgarQuery("companyfacts", "10789"),
        lambda _: _response(
            _FACTS, headers={"Transfer-Encoding": "chunked", "Content-Length": str(len(_FACTS))}
        ),
    )
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "invalid_response"
    assert outcome.body == _FACTS


class _BrokenCloseWire(SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield _FACTS + b" "

    def close(self) -> None:
        raise httpx2.CloseError("SYNTHETIC-CLOSE-PRIVATE-DIAGNOSTIC")


def test_stopping_at_body_limit_closes_without_leaking_transport_exception(
    monkeypatch, capsys
) -> None:
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    monkeypatch.setattr(sec_edgar, "_MAX_BODY_BYTES", len(_FACTS))
    raw = Response(200, stream=_BrokenCloseWire())
    outcome = _capture(SecEdgarQuery("companyfacts", "10789"), lambda _: raw)
    gc.collect()
    assert isinstance(outcome, SecEdgarReceipt)
    assert (outcome.state, outcome.status_code, outcome.body) == ("body_limit", 200, _FACTS)
    assert raw.is_closed
    assert not unraisable
    assert "SYNTHETIC-CLOSE" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "headers",
    [
        {"Transfer-Encoding": "gzip"},
        {"Transfer-Encoding": "gzip, chunked"},
        {"Content-Type": "text/html; charset=UTF-8"},
    ],
)
def test_explicit_wrong_transfer_or_media_claims_cannot_be_received(headers) -> None:
    outcome = _capture(
        SecEdgarQuery("companyfacts", "10789"), lambda _: _response(_FACTS, headers=headers)
    )
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "invalid_response"
    assert outcome.body == _FACTS


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Type": "Application/JSON; charset=utf-8"},
        {"Transfer-Encoding": "Chunked", "Content-Type": "application/json"},
    ],
)
def test_omitted_and_compatible_metadata_do_not_block_a_stream(headers) -> None:
    outcome = _capture(
        SecEdgarQuery("companyfacts", "10789"), lambda _: _response(_FACTS, headers=headers)
    )
    assert isinstance(outcome, SecEdgarReceipt)
    assert outcome.state == "received"
