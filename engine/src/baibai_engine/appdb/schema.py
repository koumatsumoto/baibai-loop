"""Current application-store schema.

Runtime code creates the current shape or rejects a different version; semantic
cutovers are explicit operator work against a verified backup.
"""

APPLICATION_SCHEMA_VERSION = 18

SCHEMA_SQL = """
CREATE TABLE task (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('open', 'done', 'dropped')),
    kind TEXT NOT NULL CHECK (kind IN ('earnings-review', 'ops', 'follow-up', 'other')),
    ticker TEXT,
    due_date TEXT NOT NULL,
    event_date TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE INDEX task_status_due_idx ON task(status, due_date, task_id);
CREATE INDEX task_ticker_idx ON task(ticker) WHERE ticker IS NOT NULL;

CREATE TABLE macro_context (
    context_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    published_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES macro_context(context_id),
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE TABLE macro_context_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    context_id TEXT NOT NULL REFERENCES macro_context(context_id)
) STRICT;
CREATE INDEX macro_context_asof_idx ON macro_context(as_of, published_at, context_id);

CREATE TABLE shortlist (
    shortlist_id TEXT PRIMARY KEY,
    selection_id TEXT NOT NULL,
    run_revision_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE INDEX shortlist_asof_idx ON shortlist(as_of, published_at);
CREATE INDEX shortlist_selection_idx ON shortlist(selection_id);

CREATE TABLE thesis (
    thesis_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    published_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES thesis(thesis_id),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    core_sha256 TEXT,
    CHECK (supersedes_id IS NULL OR supersedes_id <> thesis_id)
) STRICT;
CREATE INDEX thesis_ticker_idx ON thesis(ticker, as_of, published_at, thesis_id);
CREATE TRIGGER thesis_payload_immutable
BEFORE UPDATE OF payload ON thesis
WHEN OLD.payload <> NEW.payload
BEGIN
    SELECT RAISE(ABORT, 'published thesis payload is immutable');
END;

CREATE TABLE thesis_review (
    review_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL REFERENCES thesis(thesis_id),
    reviewed_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE INDEX thesis_review_thesis_idx ON thesis_review(thesis_id, reviewed_at, review_id);

CREATE TABLE holding_review (
    holding_review_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    thesis_id TEXT NOT NULL REFERENCES thesis(thesis_id),
    candidate_thesis_id TEXT REFERENCES thesis(thesis_id),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (candidate_thesis_id IS NULL OR candidate_thesis_id <> thesis_id)
) STRICT;
CREATE INDEX holding_review_ticker_idx ON holding_review(ticker, as_of, holding_review_id);
CREATE INDEX holding_review_thesis_idx ON holding_review(thesis_id);

CREATE TABLE bargain_assessment (
    assessment_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    published_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('buy', 'no_actionable_bargain', 'defer')),
    shortlist_id TEXT NOT NULL REFERENCES shortlist(shortlist_id),
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE INDEX bargain_assessment_asof_idx
ON bargain_assessment(as_of, published_at, assessment_id);

CREATE TABLE operation_session (
    operation_id TEXT PRIMARY KEY,
    session_kind TEXT NOT NULL CHECK (
        session_kind IN ('opportunity', 'earnings-material-event')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
    as_of TEXT NOT NULL,
    ticker TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (
        (status = 'active' AND completed_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL)
    )
) STRICT;
CREATE UNIQUE INDEX operation_session_one_active_idx
ON operation_session(status) WHERE status = 'active';
CREATE INDEX operation_session_kind_completed_idx
ON operation_session(session_kind, completed_at, operation_id);
CREATE TRIGGER operation_session_completed_no_update
BEFORE UPDATE ON operation_session
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'completed operation session is immutable');
END;
CREATE TRIGGER operation_session_completed_no_delete
BEFORE DELETE ON operation_session
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'completed operation session is immutable');
END;

CREATE TABLE ledger_event (
    append_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    same_instant_order INTEGER NOT NULL CHECK (same_instant_order >= 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'opening_balance', 'contribution', 'withdrawal', 'reservation',
            'release', 'execution', 'income', 'cost', 'tax_confirmed'
        )
    ),
    ticker TEXT,
    decision_reference TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    UNIQUE (occurred_at, same_instant_order)
) STRICT;
CREATE INDEX ledger_event_replay_idx
ON ledger_event(occurred_at, same_instant_order, append_seq);
CREATE INDEX ledger_event_decision_idx
ON ledger_event(decision_reference) WHERE decision_reference IS NOT NULL;

CREATE TABLE ledger_market_price (
    ticker TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    price_yen TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    price_basis TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE TABLE ledger_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    portfolio_scope TEXT NOT NULL CHECK (portfolio_scope = 'repository_only'),
    as_of TEXT NOT NULL,
    estimated_exit_tax_rate_bps INTEGER,
    estimated_exit_tax_basis TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (
        estimated_exit_tax_rate_bps IS NULL
        OR estimated_exit_tax_rate_bps BETWEEN 0 AND 10000
    )
) STRICT;

CREATE TABLE portfolio_outcome (
    outcome_id TEXT PRIMARY KEY,
    horizon TEXT NOT NULL CHECK (horizon IN ('1y', '3y', '5y')),
    period_start_date TEXT NOT NULL,
    period_end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'unresolved')),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (period_end_date >= period_start_date)
) STRICT;
CREATE INDEX portfolio_outcome_period_idx
ON portfolio_outcome(period_end_date, horizon, outcome_id);
"""

__all__ = ["APPLICATION_SCHEMA_VERSION", "SCHEMA_SQL"]
