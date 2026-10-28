"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

JQUANTS_DAILY_BARS_DDL = """
CREATE TABLE IF NOT EXISTS jquants_daily_bars(
  ticker TEXT NOT NULL,
  traded_at TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  turnover_value REAL,
  adjustment_factor REAL,
  upper_limit TEXT,
  lower_limit TEXT,
  ex_right_type TEXT,
  market_cap_million_yen REAL,
  PRIMARY KEY (ticker, traded_at)
);

CREATE INDEX IF NOT EXISTS idx_jquants_daily_bars_traded_at
  ON jquants_daily_bars(traded_at);

"""

JQUANTS_MARKET_CALENDAR_DDL = """CREATE TABLE IF NOT EXISTS jquants_market_calendar(
  day TEXT PRIMARY KEY,
  holiday_division TEXT NOT NULL
);

"""
