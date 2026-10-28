"""Owner-declared SQLite table semantics; assembled into the shared physical cache."""

JQUANTS_MASTER_SNAPSHOTS_DDL = """CREATE TABLE IF NOT EXISTS jquants_master_snapshots(
  snapshot_date TEXT NOT NULL,
  security_code TEXT NOT NULL,
  record_json TEXT NOT NULL,
  PRIMARY KEY (snapshot_date, security_code)
);

"""
