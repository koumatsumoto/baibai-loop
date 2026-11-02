"""MCP市場補助観測の読取。公式終値、約定価格、未来の政策へ読み替えない。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL_IDENTIFIERS
from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture


def read_market_observation(capture: MarketToolCapture) -> dict[str, Any]:
    result = capture.result
    base: dict[str, Any] = {
        "receipt_id": capture.receipt_id,
        "capture_sha256": capture.digest(),
        "available_at": capture.received_at_utc.astimezone(UTC).isoformat(),
        "request_parameters": capture.request_parameters,
        "tool_name": capture.tool_name,
        "formal_proposal_price": False,
    }
    if result.is_error:
        return {**base, "status": "failed", "observations": []}
    payload = result.structured_content
    if payload is None:
        texts = [block.text for block in result.content if block.type == "text"]
        if len(texts) != 1:
            raise ValueError("market result needs one native JSON envelope")
        payload = json.loads(texts[0])
    if not isinstance(payload, dict):
        raise ValueError("market result must be a mapping")
    if payload.get("success") is not True:
        return {**base, "status": "failed", "observations": []}
    if capture.tool_name in BATCH_TOOL_IDENTIFIERS:
        data, missing = payload.get("data"), payload.get("missing")
        symbols = capture.request_parameters["symbols"]
        if (
            not isinstance(data, dict)
            or not isinstance(missing, list)
            or any(not isinstance(symbol, str) for symbol in missing)
            or set(data) & set(missing)
            or set(data) | set(missing) != set(symbols)
        ):
            raise ValueError("market quote symbols differ from the exact request")
        quotes = []
        for symbol in symbols:
            if symbol in missing:
                quotes.append({"symbol": symbol, "status": "missing"})
                continue
            quote = data[symbol]
            if not isinstance(quote, dict):
                raise ValueError("market quote fields must be a mapping")
            mode = quote.get("update_mode")
            delay = None
            if isinstance(mode, str) and mode.startswith("delayed_streaming_"):
                suffix = mode.removeprefix("delayed_streaming_")
                delay = int(suffix) if suffix.isascii() and suffix.isdecimal() else None
            quotes.append(
                {
                    "symbol": symbol,
                    "status": "received",
                    "fields": quote,
                    "update_mode": mode,
                    "delay_seconds": delay,
                    "last_trade_at": None,
                }
            )
        return {**base, "status": "received", "observations": quotes}
    args = capture.request_parameters
    bars = payload.get("bars")
    if (
        payload.get("symbol") != args["symbol"]
        or payload.get("interval") != args["interval"]
        or payload.get("format") != "rows"
        or not isinstance(bars, list)
        or len(bars) > args["count"]
        or payload.get("count") != len(bars)
    ):
        raise ValueError("market bars differ from the exact request")
    times = []
    for bar in bars:
        if (
            not isinstance(bar, dict)
            or not {"t", "o", "h", "l", "c", "v"} <= set(bar)
            or type(bar.get("t")) is not int
        ):
            raise ValueError("market bar requires its provider UTC bucket time")
        times.append(bar["t"])
    if times != sorted(set(times)):
        raise ValueError("market bar clocks are repeated or out of order")
    return {
        **base,
        "status": "received",
        "symbol": args["symbol"],
        "interval": args["interval"],
        "bars": bars,
        "last_bar_at": datetime.fromtimestamp(times[-1], UTC).isoformat() if times else None,
        "last_bar_may_change": bool(bars),
        "notice": payload.get("notice"),
        "has_more": payload.get("has_more"),
        "update_mode": None,
        "price_adjustment": "provider_notice_only",
        "last_trade_at": None,
    }
