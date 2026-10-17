"""Physical contract for retained EDINET document-list response bytes."""

EDINET_LIST_RESPONSES_DDL = """
CREATE TABLE IF NOT EXISTS edinet_list_responses(
  receipt_id TEXT NOT NULL PRIMARY KEY,
  doc_date TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  body_gzip BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edinet_list_responses_date_received
  ON edinet_list_responses(doc_date, received_at_utc);
"""
