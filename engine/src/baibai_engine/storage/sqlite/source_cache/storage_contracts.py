"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

SOURCE_COVERAGE_DDL = """CREATE TABLE IF NOT EXISTS source_coverage(
  source TEXT NOT NULL,
  coverage_key TEXT NOT NULL,
  coverage_start TEXT,
  coverage_end TEXT,
  fetched_at_utc TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  PRIMARY KEY (source, coverage_key)
);

CREATE INDEX IF NOT EXISTS idx_source_coverage_source_window
  ON source_coverage(source, coverage_start, coverage_end);

"""

LAKE_STORE_ORIGIN_DDL = """CREATE TABLE IF NOT EXISTS lake_store_origin(
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  release_id TEXT NOT NULL,
  release_manifest_sha256 TEXT NOT NULL
    CHECK (length(release_manifest_sha256) = 64)
);

"""
