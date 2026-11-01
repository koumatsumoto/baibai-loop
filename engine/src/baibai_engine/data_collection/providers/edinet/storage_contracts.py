"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

EDINET_SOURCE_ARCHIVES_DDL = """CREATE TABLE IF NOT EXISTS edinet_source_archives(
  doc_id TEXT NOT NULL,
  source_type INTEGER NOT NULL,
  stored_on TEXT NOT NULL,
  zip_content BLOB NOT NULL,
  PRIMARY KEY (doc_id, source_type)
);

"""

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
