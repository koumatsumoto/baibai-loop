"""Store rebuildable Security Analyses and Review Sets."""

RUN_STORE_SCHEMA_VERSION = 5

SCHEMA_SQL = """
CREATE TABLE screening_run (
    run_revision_id TEXT PRIMARY KEY,
    public_run_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_at TEXT NOT NULL,
    universe_size INTEGER NOT NULL CHECK (universe_size >= 0),
    rules_ref TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (public_run_id, run_at)
) STRICT;
CREATE INDEX screening_run_asof_idx
ON screening_run (asof_date, run_at, run_revision_id);
CREATE TABLE security_analysis (
    run_revision_id TEXT NOT NULL REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    ticker TEXT NOT NULL,
    sector_33 TEXT,
    per_forward REAL,
    per_trailing REAL,
    pbr REAL,
    dividend_yield REAL,
    er_annual REAL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_revision_id, ticker),
    UNIQUE (run_revision_id, ordinal)
) STRICT, WITHOUT ROWID;
CREATE INDEX security_analysis_ticker_idx
ON security_analysis (ticker, run_revision_id);
CREATE TABLE review_set (
    review_set_id TEXT PRIMARY KEY,
    run_revision_id TEXT NOT NULL REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
) STRICT;
CREATE INDEX review_set_lookup_idx
ON review_set (run_revision_id, created_at, review_set_id);
"""

__all__ = ["RUN_STORE_SCHEMA_VERSION", "SCHEMA_SQL"]
