"""有限MCP市場応答の合成入力。実価格・実受領ではない。"""

from datetime import UTC, datetime, timedelta

from mcp.types import CallToolResult

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture

CLOCK = datetime(2026, 1, 5, 4, tzinfo=UTC)


def capture(*, receipt_id="synthetic-market", bars=False, received=CLOCK, error=False):
    if bars:
        args = {"symbol": "CME:SYNTH", "interval": "5m", "count": 2, "summary": False}
        payload = {
            "success": True,
            "symbol": "CME:SYNTH",
            "interval": "5m",
            "format": "rows",
            "count": 2,
            "has_more": True,
            "notice": "synthetic: last bar may change",
            "bars": [
                {
                    "t": int((received - timedelta(minutes=20)).timestamp()),
                    "o": 10,
                    "h": 11,
                    "l": 9,
                    "c": 10,
                    "v": 0,
                },
                {
                    "t": int((received - timedelta(minutes=15)).timestamp()),
                    "o": 10,
                    "h": 12,
                    "l": 9,
                    "c": 11,
                    "v": None,
                },
            ],
        }
        tool = "mcp-tv-get-ohlcv"
    else:
        args = {
            "symbols": ["CME:SYNTH", "TSE:SYNTH"],
            "columns": ["close", "change", "update_mode"],
        }
        payload = {
            "success": True,
            "count": 1,
            "data": {
                "CME:SYNTH": {"close": 10, "change": 0, "update_mode": "delayed_streaming_600"}
            },
            "missing": ["TSE:SYNTH"],
            "missing_count": 1,
        }
        tool = BATCH_TOOL
    result = CallToolResult.model_validate(
        {
            "content": [
                {"type": "text", "text": "synthetic native error" if error else "native text"}
            ],
            "structuredContent": None if error else payload,
            "isError": error,
            "_meta": {"synthetic": True},
        }
    )
    return MarketToolCapture(
        receipt_id=receipt_id,
        tool_name=tool,
        requested_at_utc=received - timedelta(seconds=2),
        received_at_utc=received,
        request_parameters=args,
        result=result,
    )
