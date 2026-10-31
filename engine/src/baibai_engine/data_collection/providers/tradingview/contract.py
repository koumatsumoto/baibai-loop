"""Physical L1 contract for received TradingView MCP tool results."""

BATCH_TOOL = "mcp-tv-get-symbol-data-batch"
# The connected app exposes a qualified identifier for the same explicit batch schema.
# Retain the identifier actually called, including when replaying native wire receipts.
OFFICIAL_CONNECTOR_BATCH_TOOL = (
    "mcp__codex_apps__tradingview_official_mcp_server_mcp_tv_get_symbol_data_batch"
)
BATCH_TOOL_IDENTIFIERS = frozenset({BATCH_TOOL, OFFICIAL_CONNECTOR_BATCH_TOOL})

TRADINGVIEW_SOURCE_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS tradingview_source_receipts(
  receipt_id TEXT NOT NULL PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  master_asof TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  requested_symbols_json TEXT NOT NULL,
  requested_columns_json TEXT NOT NULL,
  result_json TEXT NOT NULL
);
"""
