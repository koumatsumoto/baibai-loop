"""Store rebuildable machine output that produces L1 shortlist candidates."""

RUN_STORE_SCHEMA_VERSION = 4

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
    application_git_commit TEXT,
    UNIQUE (public_run_id, run_at)
) STRICT;
CREATE INDEX screening_run_asof_idx
ON screening_run (asof_date, run_at, run_revision_id);
CREATE TABLE screening_candidate (
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
CREATE INDEX screening_candidate_ticker_idx
ON screening_candidate (ticker, run_revision_id);
CREATE TABLE screening_selection (
    selection_id TEXT PRIMARY KEY,
    run_revision_id TEXT NOT NULL REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
    profile TEXT NOT NULL,
    macro_context_id TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    application_git_commit TEXT
) STRICT;
CREATE INDEX screening_selection_lookup_idx
ON screening_selection (run_revision_id, profile, created_at, selection_id);
CREATE TABLE selection_entry (
    selection_id TEXT NOT NULL
        REFERENCES screening_selection(selection_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    ticker TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    payload TEXT NOT NULL,
    PRIMARY KEY (selection_id, ordinal),
    UNIQUE (selection_id, ticker)
) STRICT, WITHOUT ROWID;
"""

__all__ = ["RUN_STORE_SCHEMA_VERSION", "SCHEMA_SQL"]
