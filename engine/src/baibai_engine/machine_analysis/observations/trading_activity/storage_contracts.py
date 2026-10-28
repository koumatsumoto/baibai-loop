"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

JQUANTS_WEEKLY_MARGIN_DDL = """-- Margin balances the exchange publishes once a week, as of that week's balance
-- date. `issue_type` is kept because it decides whether a short balance is even
-- possible: a 信用銘柄 (1) has no stock lending, so its zero short balance is a
-- property of the instrument rather than an observation about positioning, and
-- an axis built on the long/short ratio has to say which it is looking at.
-- Standard and negotiable margin are stored separately because only the standard
-- side carries a six-month settlement deadline.
CREATE TABLE IF NOT EXISTS jquants_weekly_margin(
  week_end TEXT NOT NULL CHECK (week_end <= '2026-09-18'),
  ticker TEXT NOT NULL,
  long_vol REAL,
  short_vol REAL,
  long_std_vol REAL,
  long_neg_vol REAL,
  short_std_vol REAL,
  short_neg_vol REAL,
  issue_type TEXT,
  PRIMARY KEY (week_end, ticker)
);

CREATE INDEX IF NOT EXISTS idx_jquants_weekly_margin_ticker
  ON jquants_weekly_margin(ticker, week_end);

"""

JQUANTS_MARGIN_ALERTS_DDL = """-- Daily balances for the limited set of 日々公表銘柄. This source includes the
-- exchange's margin-regulation classification and remains distinct from both
-- the legacy weekly all-issues series and its daily all-issues replacement.
CREATE TABLE IF NOT EXISTS jquants_margin_alerts(
  publication_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  -- AppDate is the reported balance's application date (申込日), not a regulation effective date.
  applied_date TEXT NOT NULL,
  publication_reason TEXT,
  short_outstanding REAL,
  short_change REAL,
  short_ratio REAL,
  long_outstanding REAL,
  long_change REAL,
  long_ratio REAL,
  short_long_ratio REAL,
  short_negotiable_outstanding REAL,
  short_negotiable_change REAL,
  short_standard_outstanding REAL,
  short_standard_change REAL,
  long_negotiable_outstanding REAL,
  long_negotiable_change REAL,
  long_standard_outstanding REAL,
  long_standard_change REAL,
  tse_margin_regulation_classification TEXT,
  PRIMARY KEY (publication_date, ticker, applied_date)
);

CREATE INDEX IF NOT EXISTS idx_jquants_margin_alerts_ticker
  ON jquants_margin_alerts(ticker, publication_date);

"""

JQUANTS_ALL_ISSUES_DAILY_MARGIN_DDL = """-- All-issues daily balances beginning with the 2026-09-25 balance. A separate
-- table is a semantic boundary: these rows must never be read as weekly history.
CREATE TABLE IF NOT EXISTS jquants_all_issues_daily_margin(
  balance_date TEXT NOT NULL CHECK (balance_date >= '2026-09-25'),
  ticker TEXT NOT NULL,
  long_vol REAL,
  short_vol REAL,
  long_std_vol REAL,
  long_neg_vol REAL,
  short_std_vol REAL,
  short_neg_vol REAL,
  issue_type TEXT,
  publication_date TEXT,
  long_value REAL,
  short_value REAL,
  long_std_value REAL,
  long_neg_value REAL,
  short_std_value REAL,
  short_neg_value REAL,
  PRIMARY KEY (balance_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_jquants_all_issues_daily_margin_ticker
  ON jquants_all_issues_daily_margin(ticker, balance_date);

"""

JQUANTS_SHORT_SALE_REPORTS_DDL = """-- Investor-level positions disclosed under the 0.5% short-position reporting
-- rule. Names remain the provider's exact reporter identity fields: guessing
-- corporate identity across spelling changes would turn source facts into an
-- entity-resolution estimate. Disclosure and calculation dates are separate so
-- a historical panel can enforce both sides of the point-in-time boundary.
CREATE TABLE IF NOT EXISTS jquants_short_sale_reports(
  disclosed_at TEXT NOT NULL,
  source_ordinal INTEGER NOT NULL,
  calculated_at TEXT NOT NULL,
  ticker TEXT NOT NULL,
  short_seller_name TEXT NOT NULL,
  discretionary_investment_contractor_name TEXT NOT NULL,
  investment_fund_name TEXT NOT NULL,
  short_ratio REAL,
  short_shares INTEGER,
  short_trading_units INTEGER,
  previous_reported_at TEXT,
  previous_short_ratio REAL,
  notes TEXT,
  short_seller_address TEXT,
  discretionary_investment_contractor_address TEXT,
  PRIMARY KEY (disclosed_at, source_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_jquants_short_sale_reports_ticker
  ON jquants_short_sale_reports(ticker, disclosed_at, calculated_at);

"""
