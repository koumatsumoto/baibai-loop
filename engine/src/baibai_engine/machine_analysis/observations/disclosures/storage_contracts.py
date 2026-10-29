"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

JPX_EARNINGS_CALENDAR_DDL = """CREATE TABLE IF NOT EXISTS jpx_earnings_calendar(
  announcement_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  PRIMARY KEY (announcement_date, ticker)
);

"""

EDINET_DOCUMENTS_DDL = """CREATE TABLE IF NOT EXISTS edinet_documents(
  doc_date TEXT NOT NULL,
  sequence_number INTEGER NOT NULL,
  doc_id TEXT NOT NULL,
  sec_code TEXT,
  doc_type_code TEXT,
  csv_flag TEXT,
  xbrl_flag TEXT,
  legal_status TEXT,
  disclosure_status TEXT,
  withdrawal_status TEXT,
  doc_info_edit_status TEXT,
  parent_doc_id TEXT,
  operation_datetime TEXT,
  submit_datetime TEXT,
  doc_description TEXT,
  period_start TEXT,
  period_end TEXT,
  edinet_code TEXT,
  issuer_edinet_code TEXT,
  subject_edinet_code TEXT,
  PRIMARY KEY (doc_date, sequence_number)
);

"""

EDINET_DOCUMENT_LISTS_DDL = """CREATE TABLE IF NOT EXISTS edinet_document_lists(
  doc_date TEXT PRIMARY KEY,
  process_datetime TEXT,
  result_count INTEGER NOT NULL,
  selected_received_at_utc TEXT,
  response_json TEXT,
  selected_receipt_id TEXT,
  latest_received_at_utc TEXT NOT NULL,
  rechecked_asof TEXT
);

"""

JPX_REGULATION_FLAGS_DDL = """CREATE TABLE IF NOT EXISTS jpx_regulation_flags(
  asof_date TEXT NOT NULL,
  source_name TEXT NOT NULL,
  ticker TEXT NOT NULL,
  flag TEXT NOT NULL,
  fetched_at_utc TEXT,
  PRIMARY KEY (asof_date, source_name, ticker, flag)
);

"""

JPX_REGULATION_SOURCES_DDL = """CREATE TABLE IF NOT EXISTS jpx_regulation_sources(
  asof_date TEXT NOT NULL,
  source_name TEXT NOT NULL,
  fetched_at_utc TEXT,
  PRIMARY KEY (asof_date, source_name)
);

"""

TSE_CAPITAL_POLICY_SNAPSHOTS_DDL = """CREATE TABLE IF NOT EXISTS tse_capital_policy_snapshots(
  snapshot_month_end TEXT NOT NULL,
  ticker TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('disclosed', 'considering')),
  status_change TEXT,
  updated_on TEXT,
  contact_requested INTEGER NOT NULL,
  first_disclosed_month_end TEXT,
  first_disclosure_left_censored INTEGER NOT NULL,
  PRIMARY KEY (snapshot_month_end, ticker)
);

CREATE INDEX IF NOT EXISTS idx_tse_capital_policy_snapshots_ticker
  ON tse_capital_policy_snapshots(ticker, snapshot_month_end);

"""

JPX_DELISTINGS_DDL = """CREATE TABLE IF NOT EXISTS jpx_delistings(
  delisted_on TEXT NOT NULL,
  ticker TEXT NOT NULL,
  name TEXT NOT NULL,
  market TEXT,
  reason TEXT NOT NULL,
  PRIMARY KEY (delisted_on, ticker)
);

"""

TENDER_OFFER_EXIT_VALUES_DDL = """CREATE TABLE IF NOT EXISTS tender_offer_exit_values(
  ticker TEXT NOT NULL,
  delisted_on TEXT NOT NULL,
  offer_price_yen REAL NOT NULL CHECK (offer_price_yen > 0),
  offer_doc_id TEXT NOT NULL,
  result_doc_id TEXT NOT NULL,
  filed_on TEXT NOT NULL,
  PRIMARY KEY (ticker, delisted_on)
);
"""
