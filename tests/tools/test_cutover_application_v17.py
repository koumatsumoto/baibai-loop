from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from baibai_engine.appdb.schema import SCHEMA_SQL

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/migrations/cutover_application_v17.py"


def test_cutover_preserves_rows_and_removes_proposal_storage(tmp_path: Path) -> None:
    source = tmp_path / "v16.sqlite"
    output = tmp_path / "v17.sqlite"
    with sqlite3.connect(source) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "ALTER TABLE ledger_event RENAME COLUMN decision_reference TO proposal_id"
        )
        connection.execute(
            "CREATE TABLE proposal (proposal_id TEXT PRIMARY KEY, payload TEXT NOT NULL) STRICT"
        )
        shortlist = {
            "schema_version": 5,
            "kind": "shortlist",
            "shortlist_id": "shortlist-20260829-cutover-test",
            "selection_id": "selection-cutover-test",
            "run_revision_id": "run-cutover-test",
            "as_of": "2026-08-29",
            "published_at": "2026-08-29T12:00:00+09:00",
            "profile": "production",
            "macro_context_id": None,
            "review_basis_shortlist_id": None,
            "research_gate_contract_id": "research-gate-v1",
            "attention_policy_id": "retired",
            "attention_policy_hash": "retired",
            "attention_policy_parameters": {},
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "rejected",
                    "reason": "research slot is not justified",
                    "reject_class": "other",
                    "rank": None,
                    "narrative": None,
                    "er_annual": 0.09,
                    "machine_snapshot": None,
                }
            ],
        }
        connection.execute(
            "INSERT INTO shortlist VALUES (?, ?, ?, ?, ?, ?)",
            (
                shortlist["shortlist_id"],
                shortlist["selection_id"],
                shortlist["run_revision_id"],
                shortlist["as_of"],
                shortlist["published_at"],
                json.dumps(shortlist),
            ),
        )
        connection.execute(
            "INSERT INTO ledger_event VALUES (1, 'event-1', '2026-08-29T12:00:00+09:00', "
            "0, 'reservation', '2331', 'proposal-old', '{}')"
        )
        connection.execute("INSERT INTO proposal VALUES ('proposal-old', '{}')")
        connection.execute("PRAGMA user_version = 16")
        connection.commit()

    completed = subprocess.run(
        [sys.executable, str(TOOL), "--source", str(source), "--output", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (17,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT decision_reference, payload FROM ledger_event"
        ).fetchone() == ("proposal-old", "{}")
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'proposal'"
            ).fetchone()
            is None
        )
        payload = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])
        assert payload["schema_version"] == 6
        assert "attention_policy_id" not in payload
