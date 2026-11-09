"""MCP市場補助観測の読取。公式終値、約定価格、未来の政策へ読み替えない。"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.tradingview.accounting import account_batch
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
        symbols = capture.request_parameters["symbols"]
        data, missing = account_batch(payload, symbols)
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
    try:
        last_bar_at = datetime.fromtimestamp(times[-1], UTC).isoformat() if times else None
    except (OverflowError, OSError) as error:
        raise ValueError("market bar timestamp is outside the supported range") from error
    return {
        **base,
        "status": "received",
        "symbol": args["symbol"],
        "interval": args["interval"],
        "bars": bars,
        "last_bar_at": last_bar_at,
        "last_bar_may_change": bool(bars),
        "notice": payload.get("notice"),
        "has_more": payload.get("has_more"),
        "update_mode": None,
        "price_adjustment": "provider_notice_only",
        "last_trade_at": None,
    }


def stored_market_context(path: Path, *, asof: date, known_at: datetime) -> dict[str, Any]:
    """Project bounded, already-received captures; no fetch or trade-price substitution.

    Each source retains its own receipt and market clocks. Newer failed evidence
    stays failed rather than silently reviving an older successful value. The
    cutoff is also bounded by the end of the as-of day in JST: when as-of is the
    previous trading day, this excludes overnight captures even if known_at is
    later. It does not prove that an older observation is still current.
    """
    from baibai_engine.data_collection.providers.tradingview.market_models import (
        MARKET_SOURCE_ID,
        OHLCV_TOOL_IDENTIFIERS,
    )
    from baibai_engine.data_collection.providers.tradingview.market_receipts import (
        decode_market_capture,
    )
    from baibai_engine.primitives.time import JST
    from baibai_engine.storage.sqlite.read import connect_read_only

    if known_at.utcoffset() is None:
        raise ValueError("market context needs an as-of date and timezone-aware cutoff")
    if not path.is_file():
        return {"coverage": "not_observed", "quotes": [], "bars": []}
    end = datetime.combine(asof + timedelta(days=1), time.min, JST).astimezone(UTC)
    cutoff = known_at.astimezone(UTC)
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT request_key,fetched_at,source_url,content_type,payload,metadata "
            "FROM economic_source_documents WHERE source_id=? AND fetched_at<=? AND fetched_at<? "
            "ORDER BY fetched_at DESC,request_key DESC LIMIT 201",
            (
                MARKET_SOURCE_ID,
                cutoff.isoformat(timespec="microseconds"),
                end.isoformat(timespec="microseconds"),
            ),
        ).fetchall()
    except sqlite3.OperationalError as error:
        # An unwritten supplemental source does not erase other market readings.
        if "no such table: economic_source_documents" not in str(error):
            raise
        return {"coverage": "not_observed", "quotes": [], "bars": []}
    finally:
        connection.close()

    bounded = len(rows) > 200
    quotes: dict[str, dict[str, Any]] = {}
    bars: dict[str, dict[str, Any]] = {}
    invalid = 0
    for receipt_id, received, source_url, content_type, payload, raw_metadata in rows[:200]:
        try:
            capture = decode_market_capture(
                receipt_id, received, source_url, content_type, payload, raw_metadata
            )
        except (TypeError, ValueError, KeyError):
            invalid += 1
            continue
        try:
            observation = read_market_observation(capture)
        except (TypeError, ValueError, KeyError):
            # Its request still identifies the attempted symbols. Keep this
            # reception as their latest failure rather than reviving old prices.
            observation = {
                "status": "invalid_payload",
                "available_at": capture.received_at_utc.astimezone(UTC).isoformat(),
            }
        args = capture.request_parameters
        if capture.tool_name in BATCH_TOOL_IDENTIFIERS:
            by_symbol = {item["symbol"]: item for item in observation.get("observations", [])}
            for symbol in args["symbols"]:
                item = by_symbol.get(symbol, {"symbol": symbol, "status": observation["status"]})
                fields = item.get("fields", {})
                close = fields.get("close") if isinstance(fields, dict) else None
                if (
                    item["status"] == "received"
                    and "close" in args["columns"]
                    and not _positive_finite(close)
                ):
                    item = {"symbol": symbol, "status": "missing_or_invalid_close"}
                candidate = {
                    **item,
                    "receipt_id": receipt_id,
                    "capture_sha256": capture.digest(),
                    "received_at": observation["available_at"],
                }
                _keep_latest(
                    quotes,
                    symbol,
                    candidate,
                    compare=("status", "fields", "update_mode", "delay_seconds", "last_trade_at"),
                )
        elif capture.tool_name in OHLCV_TOOL_IDENTIFIERS:
            symbol = args["symbol"]
            values = observation.get("bars", [])
            last = values[-1] if values else None
            status = observation["status"]
            if last is None and status == "received":
                status = "missing_bars"
            elif last is not None and last["t"] > int(capture.received_at_utc.timestamp()):
                last, status = None, "future_bar_timestamp"
            elif last is not None and not all(
                _positive_finite(last.get(key)) for key in ("o", "h", "l", "c")
            ):
                last, status = None, "invalid_bar_values"
            elif last is not None and not (
                last["l"] <= min(last["o"], last["c"]) <= max(last["o"], last["c"]) <= last["h"]
            ):
                last, status = None, "inconsistent_bar_range"
            candidate = {
                "symbol": symbol,
                "status": status,
                "receipt_id": receipt_id,
                "capture_sha256": capture.digest(),
                "received_at": observation["available_at"],
                "last_bar_at": observation.get("last_bar_at") if last is not None else None,
                "last_bar": last,
                "last_bar_may_change": bool(last),
                "price_adjustment": observation.get("price_adjustment"),
            }
            _keep_latest(
                bars,
                symbol,
                candidate,
                compare=("status", "last_bar_at", "last_bar", "price_adjustment"),
            )
    partial = (
        invalid
        or bounded
        or not quotes
        or not bars
        or any(x["status"] != "received" for x in [*quotes.values(), *bars.values()])
    )
    return {
        "coverage": "not_observed" if not rows else "partial" if partial else "received",
        "formal_proposal_price": False,
        "as_of": asof.isoformat(),
        "known_at": cutoff.isoformat(),
        "history_bounded": bounded,
        "unidentified_invalid_receipts": invalid,
        "quotes": list(quotes.values()),
        "bars": list(bars.values()),
        "interpretation": (
            "supplemental_provider_observations_not_official_close_or_execution_price"
        ),
    }


def _positive_finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _keep_latest(
    selected: dict[str, dict[str, Any]],
    symbol: str,
    candidate: dict[str, Any],
    *,
    compare: tuple[str, ...],
) -> None:
    previous = selected.get(symbol)
    if previous is None:
        selected[symbol] = candidate
    elif previous["received_at"] == candidate["received_at"] and any(
        previous.get(key) != candidate.get(key) for key in compare
    ):
        previous_ids = previous.get("conflicting_receipt_ids", [previous.get("receipt_id")])
        selected[symbol] = {
            "symbol": symbol,
            "status": "ambiguous_same_time",
            "received_at": candidate["received_at"],
            "conflicting_receipt_ids": [*previous_ids, candidate["receipt_id"]],
        }
