"""Append-only native projections and independent immutable extraction results."""

ARTICLE_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS article_projection_runs(
  run_id TEXT NOT NULL PRIMARY KEY,
  receipt_id TEXT NOT NULL,
  generated_on TEXT NOT NULL,
  run_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS article_observations(
  observation_id TEXT NOT NULL PRIMARY KEY,
  article_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  received_on TEXT NOT NULL,
  observation_sha256 TEXT NOT NULL,
  observation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS article_extraction_artifacts(
  artifact_id TEXT NOT NULL PRIMARY KEY,
  observation_id TEXT NOT NULL,
  generated_on TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  artifact_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_observations_article ON article_observations(article_id);
CREATE INDEX IF NOT EXISTS idx_article_artifacts_observation
  ON article_extraction_artifacts(observation_id);
"""
