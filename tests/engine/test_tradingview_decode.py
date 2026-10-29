"""Interpret retained SDK envelopes without a client, OAuth, or network."""

import pytest
from mcp.types import CallToolResult, TextContent

from baibai_engine.machine_analysis.observations.external_assessments.tradingview.decode import (
    decode_tool_result,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
    ProviderHTTPError,
    ProviderPayloadError,
)

SCANNER_ERROR = (
    "tradingview api: https://scanner.tradingview.com/japan/scan?label-product=tv-mcp: 429"
)


def decoded(result: CallToolResult) -> dict:
    return decode_tool_result(result.model_dump_json(by_alias=True))


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("HTTP 429 private-token https://secret", 429),
        ("status code 503 private-token", 503),
        ("status 401", 401),
        ("HTTP 403 HTTP 403", 403),
        ("429 private-token", None),
        ("HTTP 429 status code 503 private-token", None),
        ("HTTP 999", None),
        ("HTTP 4290", None),
        (SCANNER_ERROR, 429),
        (SCANNER_ERROR + ": ", 429),
    ],
)
def test_saved_tool_error_preserves_unambiguous_status_not_payload(text, status):
    result = CallToolResult(isError=True, content=[TextContent(type="text", text=text)])
    with pytest.raises(FetchError) as caught:
        decoded(result)
    assert getattr(caught.value, "status_code", None) == status
    assert isinstance(caught.value, ProviderHTTPError) == (status is not None)
    assert "private-token" not in str(caught.value)
    assert "https://secret" not in str(caught.value)


@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize("error", [SCANNER_ERROR, SCANNER_ERROR + ": ", "HTTP 429 private-token"])
def test_unsuccessful_saved_envelope_has_the_same_error_meaning(structured, error):
    import json

    payload = {"success": False, "error": error, "token": "private-token"}
    result = CallToolResult(
        isError=False,
        structuredContent=payload if structured else None,
        content=[TextContent(type="text", text=json.dumps(payload))],
    )
    with pytest.raises(ProviderHTTPError) as caught:
        decoded(result)
    assert caught.value.status_code == 429
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": "429 private-token"},
        {"success": False, "error": "HTTP 429 status code 503"},
        {"success": False, "error": {"message": "HTTP 429"}},
        {"success": False, "error": SCANNER_ERROR.replace("scanner.tradingview.com", "secret")},
        {"success": False, "error": SCANNER_ERROR + "0"},
        {"success": False, "error": SCANNER_ERROR.replace(": 429", ": 200")},
        {"success": False, "message": "HTTP 429"},
        {"error": "HTTP 429"},
        {"success": 0, "error": "HTTP 429"},
    ],
)
def test_unrecognized_saved_envelope_stays_invalid_without_text_fallback(payload):
    result = CallToolResult(
        isError=False,
        structuredContent=payload,
        content=[TextContent(type="text", text='{"success":true,"data":{}}')],
    )
    with pytest.raises(ProviderPayloadError) as caught:
        decoded(result)
    assert caught.value.reason == "malformed_envelope"


def test_successful_saved_envelope_does_not_extract_status_from_an_unrelated_field():
    payload = {"success": True, "data": {}, "error": SCANNER_ERROR}
    assert decoded(CallToolResult(isError=False, structuredContent=payload, content=[])) == payload
