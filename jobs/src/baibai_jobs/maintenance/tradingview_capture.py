"""Retain an explicitly captured official MCP result, without network or credentials."""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Annotated

from mcp.types import CallToolResult
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from baibai_engine.data_collection.providers.tradingview.receipts import retain_tool_result
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema
from baibai_jobs.observability.tradingview import SourceDataError

type Name = Annotated[str, Field(min_length=1)]


class NativeToolCapture(BaseModel):
    """Capture metadata is fixed at reception; result is the SDK envelope, not L2 values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: Name
    snapshot_date: date
    master_asof: date
    tool_name: Name
    received_at_utc: AwareDatetime
    requested_symbols: tuple[str, ...]
    requested_columns: tuple[str, ...]
    result: CallToolResult


def retain_capture(sqlite_path: Path, capture_path: Path) -> dict[str, object]:
    """Import to an existing current-schema work copy; do not project or publish it."""
    try:
        capture = NativeToolCapture.model_validate_json(capture_path.read_bytes())
    except (OSError, ValueError):
        raise SourceDataError("Invalid saved TradingView SDK capture") from None
    with closing(sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=rw", uri=True)) as conn:
        validate_current_schema(conn)
        receipt_id = retain_tool_result(
            conn,
            receipt_id=capture.receipt_id,
            snapshot_date=capture.snapshot_date,
            master_asof=capture.master_asof,
            tool_name=capture.tool_name,
            received_at_utc=capture.received_at_utc,
            requested_symbols=capture.requested_symbols,
            requested_columns=capture.requested_columns,
            result=capture.result,
        )
    return {
        "receipt_id": receipt_id,
        "snapshot_date": capture.snapshot_date.isoformat(),
        "l1_retained": True,
        "tool_is_error": capture.result.is_error,
        "l2_updated": False,
    }
