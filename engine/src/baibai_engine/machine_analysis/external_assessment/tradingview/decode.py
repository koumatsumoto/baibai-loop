"""Pure interpretation of a retained TradingView MCP SDK result."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from mcp.types import CallToolResult

from baibai_engine.machine_analysis.external_assessment.tradingview.observations import (
    FetchError,
    ProviderHTTPError,
    ProviderPayloadError,
)


def _provider_http_status(texts: Iterable[str]) -> int | None:
    statuses: set[int] = set()
    for text in texts:
        statuses.update(
            int(match)
            for match in re.findall(
                r"\b(?:HTTP|status(?: code)?)\s+([1-5][0-9]{2})\b", text, re.IGNORECASE
            )
        )
        match = re.fullmatch(
            r"tradingview api: https://scanner\.tradingview\.com/[a-z]+/scan"
            r"\?label-product=tv-mcp: ([45][0-9]{2})(?::[^\r\n]*)?",
            text,
        )
        if match is not None:
            statuses.add(int(match[1]))
    return next(iter(statuses)) if len(statuses) == 1 else None


def decode_tool_result(result_json: str) -> dict[str, Any]:
    """Interpret one retained SDK result without acquisition or storage access."""
    try:
        result = CallToolResult.model_validate_json(result_json)
    except ValueError:
        raise ProviderPayloadError("malformed_envelope") from None
    if result.is_error:
        status = _provider_http_status(item.text for item in result.content if item.type == "text")
        if status is not None:
            raise ProviderHTTPError(status)
        raise FetchError("TradingView MCP tool failed")
    payload = result.structured_content
    if payload is None:
        texts = [item.text for item in result.content if item.type == "text"]
        if len(texts) != 1:
            raise ProviderPayloadError("malformed_envelope")
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError:
            raise ProviderPayloadError("malformed_envelope") from None
    if isinstance(payload, dict) and payload.get("success") is False:
        error = payload.get("error")
        if isinstance(error, str):
            status = _provider_http_status([error])
            if status is not None:
                raise ProviderHTTPError(status)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ProviderPayloadError("malformed_envelope")
    return payload
