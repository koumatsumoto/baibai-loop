"""L1 article history; no current-row replacement or retention delete path."""

ARTICLE_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS article_source_receipts(
  receipt_id TEXT NOT NULL PRIMARY KEY,
  source TEXT NOT NULL,
  received_on TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  body_gzip BLOB
);
CREATE INDEX IF NOT EXISTS idx_article_receipts_received ON article_source_receipts(received_on);
"""
