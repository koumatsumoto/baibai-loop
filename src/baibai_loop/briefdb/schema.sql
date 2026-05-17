CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider TEXT,
  tier TEXT,
  url TEXT,
  access_method TEXT,
  status_policy TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS indicators(
  indicator_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL,
  geography TEXT NOT NULL,
  unit TEXT,
  frequency TEXT NOT NULL,
  primary_source_id TEXT,
  transform_rule TEXT NOT NULL DEFAULT 'raw',
  description TEXT
);

CREATE TABLE IF NOT EXISTS indicator_aliases(
  alias TEXT PRIMARY KEY,
  indicator_id TEXT NOT NULL REFERENCES indicators(indicator_id)
);

CREATE TABLE IF NOT EXISTS indicator_observations(
  observation_id TEXT PRIMARY KEY,
  indicator_id TEXT NOT NULL REFERENCES indicators(indicator_id),
  period_start TEXT,
  period_end TEXT,
  as_of_date TEXT,
  release_date TEXT,
  value_num REAL,
  value_text TEXT,
  unit TEXT,
  source_id TEXT REFERENCES sources(source_id),
  source_url TEXT,
  fetched_at TEXT,
  vintage_at TEXT,
  fetch_status TEXT NOT NULL,
  raw_payload_hash TEXT,
  brief_path TEXT,
  note TEXT,
  CHECK (fetch_status IN ('ok', 'partial', 'failed', 'unreleased'))
);

CREATE INDEX IF NOT EXISTS idx_indicator_observations_indicator_date
  ON indicator_observations(indicator_id, as_of_date, period_end, release_date);

CREATE TABLE IF NOT EXISTS ingestion_runs(
  run_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES sources(source_id),
  adapter_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  error_message TEXT,
  raw_ref TEXT
);

CREATE TABLE IF NOT EXISTS coverage_requirements(
  kind TEXT NOT NULL,
  indicator_id TEXT NOT NULL REFERENCES indicators(indicator_id),
  required INTEGER NOT NULL DEFAULT 1,
  max_staleness_days INTEGER NOT NULL DEFAULT 10,
  PRIMARY KEY (kind, indicator_id)
);

PRAGMA user_version = 1;
