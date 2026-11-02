"""判断直前の有限MCP要求。日次外部評価の充足や正式な提案価格とは別の原受領。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any, Self

from mcp.types import CallToolResult
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL_IDENTIFIERS

MARKET_SOURCE_ID = "tradingview-market"
MARKET_GROUP = "tradingview_market"
OHLCV_TOOL_IDENTIFIERS = frozenset(
    {
        "mcp-tv-get-ohlcv",
        "mcp__codex_apps__tradingview_official_mcp_server_mcp_tv_get_ohlcv",
    }
)


class MarketToolCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str = Field(min_length=1)
    tool_name: str
    requested_at_utc: AwareDatetime
    received_at_utc: AwareDatetime
    request_parameters: dict[str, Any]
    result: CallToolResult

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.requested_at_utc > self.received_at_utc:
            raise ValueError("request follows its received result")
        args = self.request_parameters
        if self.tool_name in BATCH_TOOL_IDENTIFIERS:
            if set(args) != {"symbols", "columns"}:
                raise ValueError("market batch requires explicit symbols and columns")
            symbols, columns = args["symbols"], args["columns"]
            if (
                not isinstance(symbols, list)
                or not 1 <= len(symbols) <= 50
                or any(not isinstance(symbol, str) or ":" not in symbol for symbol in symbols)
                or len(set(symbols)) != len(symbols)
                or not isinstance(columns, list)
                or not columns
                or any(not isinstance(column, str) or not column for column in columns)
            ):
                raise ValueError("invalid explicit market batch request")
        elif self.tool_name in OHLCV_TOOL_IDENTIFIERS:
            if (
                set(args) != {"symbol", "interval", "count", "summary"}
                or not isinstance(args["symbol"], str)
                or ":" not in args["symbol"]
                or args["interval"] != "5m"
                or type(args["count"]) is not int
                or not 1 <= args["count"] <= 500
                or args["summary"] is not False
            ):
                raise ValueError("market bars require explicit 5m rows, count 1..500")
        else:
            raise ValueError("unsupported market observation tool")
        # SDKのnative JSONを保持し、NaN/Infinityをnullへ暗黙変換しない。
        self.result_bytes()
        return self

    def result_bytes(self) -> bytes:
        return json.dumps(
            self.result.model_dump(mode="python", by_alias=True, exclude_none=False),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()

    def metadata(self) -> dict[str, object]:
        return {
            "format": "tradingview-market-receipt-v1",
            "receipt_id": self.receipt_id,
            "tool_name": self.tool_name,
            "requested_at_utc": self.requested_at_utc.astimezone(UTC).isoformat(
                timespec="microseconds"
            ),
            "request_parameters": self.request_parameters,
        }

    def digest(self) -> str:
        envelope = {
            **self.metadata(),
            "received_at_utc": self.received_at_utc.astimezone(UTC).isoformat(
                timespec="microseconds"
            ),
            "result_sha256": hashlib.sha256(self.result_bytes()).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(envelope, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
