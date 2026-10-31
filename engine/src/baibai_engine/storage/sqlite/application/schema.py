"""Current application-store schema.

Runtime code creates the current shape or rejects a different version; semantic
cutovers are explicit operator work against a verified backup.
"""

APPLICATION_SCHEMA_VERSION = 28

PROPOSAL_RECORDS_SCHEMA_SQL = """
CREATE TABLE proposal_correction (
    correction_id TEXT PRIMARY KEY,
    erroneous_proposal_id TEXT UNIQUE NOT NULL REFERENCES investment_proposal(proposal_id),
    replacement_proposal_id TEXT NOT NULL REFERENCES investment_proposal(proposal_id),
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (erroneous_proposal_id <> replacement_proposal_id)
) STRICT;
CREATE TABLE proposal_human_report (
    report_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES investment_proposal(proposal_id),
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE INDEX proposal_human_report_proposal_idx
ON proposal_human_report(proposal_id, recorded_at, report_id);
CREATE TRIGGER proposal_correction_no_update BEFORE UPDATE ON proposal_correction
BEGIN SELECT RAISE(ABORT, 'proposal correction is immutable'); END;
CREATE TRIGGER proposal_correction_no_delete BEFORE DELETE ON proposal_correction
BEGIN SELECT RAISE(ABORT, 'proposal correction is immutable'); END;
CREATE TRIGGER proposal_human_report_no_update BEFORE UPDATE ON proposal_human_report
BEGIN SELECT RAISE(ABORT, 'human proposal report is immutable'); END;
CREATE TRIGGER proposal_human_report_no_delete BEFORE DELETE ON proposal_human_report
BEGIN SELECT RAISE(ABORT, 'human proposal report is immutable'); END;
"""

COMPANY_RESEARCH_SCHEMA_SQL = """
CREATE TABLE company_research (
    research_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    prior_research_id TEXT REFERENCES company_research(research_id),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    payload_sha256 TEXT NOT NULL,
    CHECK (prior_research_id IS NULL OR prior_research_id <> research_id)
) STRICT;
CREATE INDEX company_research_latest_idx
ON company_research(ticker, as_of, recorded_at, research_id);
CREATE TRIGGER company_research_immutable
BEFORE UPDATE ON company_research
BEGIN
    SELECT RAISE(ABORT, 'saved Company Research is immutable');
END;
"""

JUDGMENT_RETROSPECTIVE_SCHEMA_SQL = """
CREATE TABLE judgment_retrospective (
    retrospective_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    payload_sha256 TEXT NOT NULL
) STRICT;
CREATE INDEX judgment_retrospective_ticker_recorded_idx
ON judgment_retrospective(ticker, recorded_at, retrospective_id);
CREATE TRIGGER judgment_retrospective_immutable
BEFORE UPDATE ON judgment_retrospective
BEGIN
    SELECT RAISE(ABORT, 'saved Judgment Retrospective is immutable');
END;
"""

SCHEMA_SQL = (
    """
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

CREATE TABLE market_context_assessment (
    context_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    published_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES market_context_assessment(context_id),
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE TABLE market_context_assessment_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    context_id TEXT NOT NULL REFERENCES market_context_assessment(context_id)
) STRICT;
CREATE INDEX market_context_assessment_asof_idx
    ON market_context_assessment(as_of, published_at, context_id);

CREATE TABLE stock_assessment (
    assessment_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    stance TEXT NOT NULL CHECK (stance IN ('supported', 'uncertain', 'unsupported')),
    published_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES stock_assessment(assessment_id),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    core_sha256 TEXT NOT NULL,
    CHECK (supersedes_id IS NULL OR supersedes_id <> assessment_id)
) STRICT;
CREATE INDEX stock_assessment_latest_idx
ON stock_assessment(ticker, as_of, published_at, assessment_id);
CREATE TRIGGER stock_assessment_immutable
BEFORE UPDATE ON stock_assessment
BEGIN
    SELECT RAISE(ABORT, 'published Stock Assessment is immutable');
END;

CREATE TABLE stock_assessment_review (
    review_id TEXT PRIMARY KEY,
    assessment_id TEXT UNIQUE NOT NULL REFERENCES stock_assessment(assessment_id),
    reviewed_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT;
CREATE TRIGGER stock_assessment_review_immutable
BEFORE UPDATE ON stock_assessment_review
BEGIN
    SELECT RAISE(ABORT, 'published Assessment Review is immutable');
END;

CREATE TABLE investment_proposal (
    proposal_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    request_scope TEXT NOT NULL CHECK (
        request_scope IN ('discover_new', 'specified_stocks', 'review_positions')
    ),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    draft_sha256 TEXT NOT NULL
) STRICT;
CREATE INDEX investment_proposal_asof_idx
ON investment_proposal(as_of, published_at, proposal_id);
CREATE TRIGGER investment_proposal_immutable
BEFORE UPDATE ON investment_proposal
BEGIN
    SELECT RAISE(ABORT, 'published Investment Proposal is immutable');
END;

CREATE TABLE investment_proposal_item (
    proposal_id TEXT NOT NULL REFERENCES investment_proposal(proposal_id),
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy', 'hold', 'sell', 'no_action', 'defer')),
    assessment_id TEXT REFERENCES stock_assessment(assessment_id),
    PRIMARY KEY (proposal_id, item_index),
    UNIQUE (proposal_id, ticker)
) STRICT;
CREATE INDEX investment_proposal_item_ticker_idx
ON investment_proposal_item(ticker, proposal_id);

CREATE TABLE position_decision_reference (
    reference TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side = 'buy'),
    origin TEXT NOT NULL CHECK (origin IN ('proposal', 'retained_reference', 'manual_fact')),
    proposal_id TEXT REFERENCES investment_proposal(proposal_id),
    source_id TEXT NOT NULL,
    source_digest TEXT,
    CHECK ((origin = 'proposal' AND proposal_id IS NOT NULL)
           OR (origin IN ('retained_reference', 'manual_fact') AND proposal_id IS NULL))
) STRICT;
CREATE INDEX position_decision_reference_proposal_idx
ON position_decision_reference(proposal_id) WHERE proposal_id IS NOT NULL;

CREATE TABLE operation_session (
    operation_id TEXT PRIMARY KEY,
    session_kind TEXT NOT NULL CHECK (
        session_kind IN ('capital-allocation', 'position-review')
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
    + COMPANY_RESEARCH_SCHEMA_SQL
    + JUDGMENT_RETROSPECTIVE_SCHEMA_SQL
    + PROPOSAL_RECORDS_SCHEMA_SQL
)

__all__ = [
    "APPLICATION_SCHEMA_VERSION",
    "COMPANY_RESEARCH_SCHEMA_SQL",
    "JUDGMENT_RETROSPECTIVE_SCHEMA_SQL",
    "SCHEMA_SQL",
]
