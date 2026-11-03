"""Retain morning market context separately from daily company expectations."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import ClientSession

from baibai_engine.data_collection.providers.tradingview.client import connect
from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    read_market_observation,
)
from baibai_jobs.acquisition.tradingview.cli import credential_storage

# Capture the provider's native bars as well as quote delay metadata. The
# continuous OSE contract is not the cash index or an executable fill.
REQUESTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        BATCH_TOOL,
        {
            "symbols": ["OSE:NK2251!", "SP:SPX", "NASDAQ:NDX"],
            "columns": ["close", "change", "update_mode"],
        },
    ),
    (
        "mcp-tv-get-ohlcv",
        {"symbol": "OSE:NK2251!", "interval": "5m", "count": 500, "summary": False},
    ),
    ("mcp-tv-get-ohlcv", {"symbol": "SP:SPX", "interval": "5m", "count": 500, "summary": False}),
)


async def collect(session: ClientSession, directory: Path) -> int:
    """Write each completed response before requesting the next, including failures."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    count = 0
    for number, (name, args) in enumerate(REQUESTS):
        if number:
            await asyncio.sleep(15)
        requested = datetime.now(UTC)
        result = await session.call_tool(name, args)
        capture = MarketToolCapture(
            receipt_id=uuid4().hex,
            tool_name=name,
            requested_at_utc=requested,
            received_at_utc=datetime.now(UTC),
            request_parameters=args,
            result=result,
        )
        path = directory / f"{number}.json"
        path.write_text(capture.model_dump_json(by_alias=True), encoding="utf-8")
        path.chmod(0o600)
        count += 1
        observation = read_market_observation(capture)
        if observation["status"] != "received":
            raise RuntimeError("provider returned an error")
        if name == BATCH_TOOL and any(
            quote["status"] != "received" for quote in observation["observations"]
        ):
            raise RuntimeError("provider did not return all requested quotes")
        if name != BATCH_TOOL and not observation["bars"]:
            raise RuntimeError("provider returned no market bars")
    return count


async def run(directory: Path) -> int:
    async with connect(credential_storage(None)) as session:
        return await collect(session, directory)


def main() -> int:
    logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)
    try:
        count = asyncio.run(run(Path(sys.argv[1])))
    except Exception as exc:
        # SDK errors can contain tokens or URLs. Do not copy exception text to logs.
        print(json.dumps({"status": "failed", "type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "received", "receipts": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
