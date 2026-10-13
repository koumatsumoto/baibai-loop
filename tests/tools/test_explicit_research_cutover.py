"""21→22のcopyは旧Thesis/Review/CAA/Operationの保存値を変えない。"""

import json
import sqlite3
from hashlib import sha256

import pytest
from tests.helpers.research_triage import research_triage_payload
from tests.helpers.research_v4 import pair_payload
from tools.cutover.explicit_research_21_to_22 import cutover, table_fingerprints

from baibai_engine.research.capital_allocation import (
    StoredCapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.schema import SCHEMA_SQL


def legacy_source(path):
    triage = research_triage_payload(research_triage_id="triage-synthetic-preserved")
    thesis, review = pair_payload()
    caa = {
        "schema_version": 1,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": "capital-allocation-assessment-20260907-preserved",
        "as_of": "2026-09-07",
        "published_at": "2026-09-07T18:00:00+09:00",
        "result": "no_allocation",
        "headline": "synthetic historical",
        "research_triage_id": triage["research_triage_id"],
        "macro_context_id": None,
        "comparison": "synthetic comparison",
        "forgone": "synthetic forgone",
        "alternatives": [
            {
                "ticker": "1234",
                "thesis_id": "thesis-synthetic-preserved",
                "thesis_core_sha256": review["reviewed_thesis_sha256"],
                "thesis_review_id": review["review_id"],
                "disposition": "decline",
                "rationale": "synthetic original rationale",
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "synthetic reviewer",
            "reviewed_at": "2026-09-07T18:00:00+09:00",
            "conclusion": "pass",
            "draft_sha256": "f" * 64,
            "open_findings": [],
        },
    }
    with sqlite3.connect(path) as c:
        c.executescript(
            SCHEMA_SQL.replace(
                "research_triage_id TEXT REFERENCES research_triage(research_triage_id)",
                "research_triage_id TEXT NOT NULL REFERENCES research_triage(research_triage_id)",
            )
        )
        c.execute("PRAGMA user_version=21")
        c.execute(
            "INSERT INTO research_triage VALUES(?,?,?,?,?,?)",
            (
                triage["research_triage_id"],
                triage["review_set_id"],
                triage["run_revision_id"],
                triage["as_of"],
                triage["published_at"],
                canonical_json(triage),
            ),
        )
        c.execute(
            "INSERT INTO thesis VALUES(?,?,?,?,?,?,?,?)",
            (
                "thesis-synthetic-preserved",
                "1234",
                "2026-09-07",
                "candidate",
                "2026-09-07T18:00:00+09:00",
                None,
                canonical_json(thesis),
                review["reviewed_thesis_sha256"],
            ),
        )
        c.execute(
            "INSERT INTO thesis_review VALUES(?,?,?,?)",
            (
                review["review_id"],
                "thesis-synthetic-preserved",
                review["reviewed_at"],
                canonical_json(review),
            ),
        )
        c.execute(
            "INSERT INTO capital_allocation_assessment VALUES(?,?,?,?,?,?)",
            (
                caa["capital_allocation_assessment_id"],
                caa["as_of"],
                caa["published_at"],
                caa["result"],
                caa["research_triage_id"],
                canonical_json(caa),
            ),
        )
        c.execute(
            "INSERT INTO operation_session VALUES(?,?,?,?,?,?,?,?)",
            (
                "op-synthetic-preserved",
                "capital-allocation",
                "completed",
                "2026-09-07",
                None,
                "2026-09-07T17:00:00+09:00",
                "2026-09-07T18:00:00+09:00",
                canonical_json(
                    {"checkpoint": "synthetic historical", "artifacts": [{"kind": "legacy_note"}]}
                ),
            ),
        )
    return caa


def test_copy_cutover_preserves_all_rows_raw_payloads_and_old_review_hash(tmp_path):
    source, output = tmp_path / "source21.sqlite", tmp_path / "output22.sqlite"
    caa = legacy_source(source)
    source_bytes = source.read_bytes()
    old_review_hash = capital_allocation_draft_sha256(
        StoredCapitalAllocationAssessment.from_payload(caa)
    )
    with sqlite3.connect(source) as c:
        before = table_fingerprints(c)
    report = cutover(source, output)
    assert report["all_table_rows_unchanged"] is True
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(output) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 22
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert table_fingerprints(c) == before
        stored = json.loads(
            c.execute("SELECT payload FROM capital_allocation_assessment").fetchone()[0]
        )
        assert stored == caa
        assert (
            capital_allocation_draft_sha256(StoredCapitalAllocationAssessment.from_payload(stored))
            == old_review_hash
        )
        columns = {
            r[1]: r[3] for r in c.execute("PRAGMA table_info(capital_allocation_assessment)")
        }
        assert columns["research_triage_id"] == 0
        c.execute("PRAGMA foreign_keys=ON")
        c.execute(
            "INSERT INTO capital_allocation_assessment VALUES(?,?,?,?,?,?)",
            (
                "synthetic-user-caa",
                "2026-09-07",
                "2026-09-07T18:00:00+09:00",
                "no_allocation",
                None,
                "{}",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO capital_allocation_assessment VALUES(?,?,?,?,?,?)",
                (
                    "synthetic-invalid-caa",
                    "2026-09-07",
                    "2026-09-07T18:00:00+09:00",
                    "no_allocation",
                    "unknown-triage",
                    "{}",
                ),
            )
    assert sha256(source.read_bytes()).digest() == sha256(source_bytes).digest()


def test_cutover_refuses_existing_output_same_file_or_wrong_version(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    legacy_source(source)
    with pytest.raises(ValueError, match="new file"):
        cutover(source, source)
    output.write_bytes(b"existing evidence")
    with pytest.raises(ValueError, match="new file"):
        cutover(source, output)
    assert output.read_bytes() == b"existing evidence"
    with sqlite3.connect(source) as c:
        c.execute("PRAGMA user_version=20")
    with pytest.raises(ValueError, match="schema 21"):
        cutover(source, tmp_path / "other-output.sqlite")
    assert not (tmp_path / "other-output.sqlite").exists()
