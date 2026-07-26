CREATE TABLE IF NOT EXISTS series(
  series_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  geography TEXT NOT NULL,
  frequency TEXT NOT NULL,
  unit TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_series_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  notes TEXT,
  plausible_min REAL,
  plausible_max REAL
);

CREATE TABLE IF NOT EXISTS aliases(
  alias TEXT NOT NULL,
  series_id TEXT NOT NULL REFERENCES series(series_id),
  PRIMARY KEY(alias, series_id)
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias
  ON aliases(alias);

CREATE TABLE IF NOT EXISTS registry_series(
  series_id TEXT PRIMARY KEY REFERENCES series(series_id)
);

CREATE TABLE IF NOT EXISTS observations(
  series_id TEXT NOT NULL REFERENCES series(series_id),
  observed_at TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  value REAL NOT NULL,
  unit TEXT NOT NULL,
  vintage_at TEXT NOT NULL,
  fetch_status TEXT NOT NULL,
  source_url TEXT NOT NULL,
  PRIMARY KEY(series_id, observed_at, vintage_at),
  CHECK(fetch_status IN ('ok', 'failed', 'unreleased'))
);

CREATE INDEX IF NOT EXISTS idx_observations_series_date
  ON observations(series_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_observations_series_status_date_vintage
  ON observations(series_id, fetch_status, observed_at, vintage_at);

CREATE TRIGGER IF NOT EXISTS validate_observation_plausibility_before_insert
BEFORE INSERT ON observations
WHEN EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND (
      NEW.unit != unit
      OR (plausible_min IS NOT NULL AND NEW.value < plausible_min)
      OR (plausible_max IS NOT NULL AND NEW.value > plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;

CREATE TRIGGER IF NOT EXISTS validate_observation_plausibility_before_update
BEFORE UPDATE OF series_id, value, unit ON observations
WHEN EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND (
      NEW.unit != unit
      OR (plausible_min IS NOT NULL AND NEW.value < plausible_min)
      OR (plausible_max IS NOT NULL AND NEW.value > plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;

CREATE TRIGGER IF NOT EXISTS validate_series_contract_before_update
BEFORE UPDATE OF unit, plausible_min, plausible_max ON series
WHEN EXISTS (
  SELECT 1 FROM observations
  WHERE series_id = NEW.series_id
    AND (
      unit != NEW.unit
      OR (NEW.plausible_min IS NOT NULL AND value < NEW.plausible_min)
      OR (NEW.plausible_max IS NOT NULL AND value > NEW.plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'series contract excludes an existing observation');
END;

CREATE TABLE IF NOT EXISTS provider_runs(
  run_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  series_id TEXT NOT NULL REFERENCES series(series_id),
  range_start TEXT NOT NULL,
  range_end TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL,
  record_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  CHECK(status IN ('ok', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_provider_runs_series_range
  ON provider_runs(series_id, range_start, range_end, status);

PRAGMA user_version = 4;
