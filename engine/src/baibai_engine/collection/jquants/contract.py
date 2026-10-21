"""Physical contract for retained J-Quants response pages."""

JQUANTS_SOURCE_PAGES_DDL = """
CREATE TABLE IF NOT EXISTS jquants_source_pages(
  request_id TEXT NOT NULL,
  page_index INTEGER NOT NULL CHECK(page_index >= 0),
  source TEXT NOT NULL,
  query_json TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  received_on TEXT NOT NULL,
  body_gzip BLOB NOT NULL,
  PRIMARY KEY(request_id, page_index)
);
CREATE INDEX IF NOT EXISTS idx_jquants_source_pages_source_received
  ON jquants_source_pages(source, received_at_utc);
"""
