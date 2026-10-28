"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

EDINET_SOURCE_ARCHIVES_DDL = """CREATE TABLE IF NOT EXISTS edinet_source_archives(
  doc_id TEXT NOT NULL,
  source_type INTEGER NOT NULL,
  stored_on TEXT NOT NULL,
  zip_content BLOB NOT NULL,
  PRIMARY KEY (doc_id, source_type)
);

"""
