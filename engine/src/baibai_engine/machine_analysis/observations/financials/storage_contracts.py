"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

JQUANTS_FIN_SUMMARIES_DDL = """CREATE TABLE IF NOT EXISTS jquants_fin_summaries(
  ticker TEXT NOT NULL,
  disclosed_at TEXT NOT NULL,
  disclosure_number TEXT NOT NULL,
  document_payload TEXT NOT NULL,
  PRIMARY KEY (ticker, disclosed_at, disclosure_number)
);

"""

EDINET_SEGMENT_FACTS_DDL = """CREATE TABLE IF NOT EXISTS edinet_segment_facts(
  ticker TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  source_submit_datetime TEXT NOT NULL,
  disclosed_on TEXT NOT NULL,
  source_element TEXT NOT NULL,
  source_context TEXT NOT NULL,
  issuer_id TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT NOT NULL,
  consolidation_basis TEXT NOT NULL,
  segment_axis TEXT NOT NULL,
  segment_key TEXT NOT NULL,
  segment_name TEXT,
  segment_kind TEXT NOT NULL,
  metric TEXT NOT NULL,
  profit_basis TEXT,
  value REAL,
  currency TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  PRIMARY KEY (source_doc_id, source_context, source_element)
);

"""

EDINET_DEBT_SCHEDULE_DDL = """CREATE TABLE IF NOT EXISTS edinet_debt_schedule(
  ticker TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  source_submit_datetime TEXT NOT NULL,
  disclosed_on TEXT NOT NULL,
  source_element TEXT NOT NULL,
  source_context TEXT NOT NULL,
  issuer_id TEXT NOT NULL,
  balance_sheet_date TEXT NOT NULL,
  consolidation_basis TEXT NOT NULL,
  debt_category TEXT NOT NULL,
  due_from_months INTEGER NOT NULL,
  due_to_months INTEGER NOT NULL,
  principal REAL,
  currency TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  PRIMARY KEY (source_doc_id, balance_sheet_date, consolidation_basis,
               debt_category, due_from_months, due_to_months)
);

"""

EDINET_METRICS_DDL = """CREATE TABLE IF NOT EXISTS edinet_metrics(
  asof_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  sales_ttm REAL,
  ocf_ttm REAL,
  debt REAL,
  cash REAL,
  ebitda_ttm REAL,
  consolidation_basis TEXT,
  ttm_quality_ev_ebitda TEXT,
  ttm_quality_p_s TEXT,
  ttm_quality_pcfr TEXT,
  operating_profit_ttm REAL,
  depreciation_and_amortization_ttm REAL,
  capex_ttm REAL,
  fcf_ttm REAL,
  net_cash REAL,
  equity REAL,
  total_assets REAL,
  ttm_quality_fcf TEXT,
  ttm_quality_net_cash TEXT,
  source_doc_id TEXT,
  document_type TEXT,
  source_submit_datetime TEXT,
  source_period_start TEXT,
  source_period_end TEXT,
  capex_source TEXT,
  failure_reasons TEXT,
  extractor_revision TEXT,
  source_document_revision TEXT,
  investment_securities REAL,
  ocf_receivables_cash_effect REAL,
  ocf_inventories_cash_effect REAL,
  ocf_payables_cash_effect REAL,
  ocf_contract_liabilities_cash_effect REAL,
  ocf_advances_received_cash_effect REAL,
  ocf_other_payables_cash_effect REAL,
  capex_ppe_reported REAL,
  capex_intangible_reported REAL,
  PRIMARY KEY (asof_date, ticker)
);

"""
