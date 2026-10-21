"""Physical L1 contract for received public equity page bodies."""

EQUITY_WEB_SOURCE_RESPONSES_DDL = """
CREATE TABLE IF NOT EXISTS equity_web_source_responses(
  receipt_id TEXT NOT NULL PRIMARY KEY,
  attempt_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('monex', 'rakuten', 'kabumap')),
  requested_ticker TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('page', 'iframe')),
  parent_receipt_id TEXT,
  requested_url TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  received_on TEXT NOT NULL,
  selected_encoding TEXT NOT NULL,
  body_gzip BLOB NOT NULL,
  UNIQUE(attempt_id, role),
  CHECK(role = 'page' OR (source = 'rakuten' AND parent_receipt_id IS NOT NULL)),
  CHECK(role != 'page' OR parent_receipt_id IS NULL),
  FOREIGN KEY(parent_receipt_id) REFERENCES equity_web_source_responses(receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_equity_web_source_responses_received_on
  ON equity_web_source_responses(received_on);
"""
