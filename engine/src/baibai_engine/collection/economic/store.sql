CREATE TABLE economic_source_records(
  source_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count >= 0),
  payload BLOB NOT NULL,
  PRIMARY KEY(source_id, observed_at, fetched_at)
);

CREATE TABLE economic_source_documents(
  source_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source_url TEXT NOT NULL,
  content_type TEXT NOT NULL,
  payload BLOB NOT NULL,
  metadata TEXT NOT NULL,
  PRIMARY KEY(source_id, request_key, fetched_at)
);

CREATE TABLE economic_source_runs(
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  range_start TEXT NOT NULL,
  range_end TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ok', 'failed')),
  record_count INTEGER NOT NULL CHECK(record_count >= 0),
  error_message TEXT
);
CREATE INDEX idx_economic_source_runs_latest
  ON economic_source_runs(source_id, finished_at DESC);

PRAGMA user_version = 12;
