"""Fixtures that reshape an indicator store for schema-boundary tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_CONTRACT_TRIGGERS = (
    "validate_observation_plausibility_before_insert",
    "validate_observation_plausibility_before_update",
    "validate_series_contract_before_update",
)


def downgrade_to_previous_schema(database: Path) -> None:
    """Rebuild `observations` under the fetch_status domain that preceded 'retracted'.

    Both the merge rollout tests and the store tests need a store one version behind, and
    a copy in each would have to be edited in step with every schema move.
    """

    with sqlite3.connect(database) as connection:
        trigger_sql = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name IN "
                f"({', '.join('?' for _ in _CONTRACT_TRIGGERS)}) ORDER BY name",
                _CONTRACT_TRIGGERS,
            )
        )
        connection.executescript(
            """
            DROP TRIGGER validate_observation_plausibility_before_insert;
            DROP TRIGGER validate_observation_plausibility_before_update;
            DROP TRIGGER validate_series_contract_before_update;
            CREATE TABLE observations_previous(
              series_id TEXT NOT NULL REFERENCES series(series_id),
              observed_at TEXT NOT NULL,
              period_start TEXT,
              period_end TEXT,
              value REAL NOT NULL,
              unit TEXT NOT NULL,
              vintage_at TEXT NOT NULL,
              fetch_status TEXT NOT NULL,
              source_url TEXT NOT NULL,
              PRIMARY KEY(series_id, observed_at, vintage_at),
              CHECK(fetch_status IN ('ok', 'failed', 'unreleased'))
            );
            INSERT INTO observations_previous SELECT * FROM observations;
            DROP TABLE observations;
            ALTER TABLE observations_previous RENAME TO observations;
            CREATE INDEX idx_observations_series_date ON observations(series_id, observed_at);
            CREATE INDEX idx_observations_series_status_date_vintage
              ON observations(series_id, fetch_status, observed_at, vintage_at);
            """
        )
        for statement in trigger_sql:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 5")


__all__ = ["downgrade_to_previous_schema"]
