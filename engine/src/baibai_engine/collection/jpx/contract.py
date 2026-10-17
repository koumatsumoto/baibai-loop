"""Physical contract for bounded JPX raw acquisition attempts."""

JPX_SOURCE_BUNDLES_DDL = """
CREATE TABLE IF NOT EXISTS jpx_source_bundles(
  bundle_id TEXT NOT NULL PRIMARY KEY,
  collection_name TEXT NOT NULL,
  asof_date TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  started_on TEXT NOT NULL,
  expected_resources_json TEXT,
  completed_at_utc TEXT
);
CREATE TABLE IF NOT EXISTS jpx_source_responses(
  bundle_id TEXT NOT NULL,
  resource_name TEXT NOT NULL,
  requested_url TEXT NOT NULL,
  received_at_utc TEXT NOT NULL,
  received_on TEXT NOT NULL,
  content_type TEXT NOT NULL,
  body_gzip BLOB NOT NULL,
  PRIMARY KEY (bundle_id, resource_name),
  FOREIGN KEY (bundle_id) REFERENCES jpx_source_bundles(bundle_id)
);
CREATE INDEX IF NOT EXISTS idx_jpx_source_bundles_started_on
  ON jpx_source_bundles(started_on);
CREATE INDEX IF NOT EXISTS idx_jpx_source_responses_received_on
  ON jpx_source_responses(received_on);
"""
