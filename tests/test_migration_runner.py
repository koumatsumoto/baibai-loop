"""Contracts for the pre-deletion migration runner only."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.holding_review import CanonicalSource, HoldingReviewDocument
from baibai_engine.position.importer import import_and_check_ledger
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService
from baibai_engine.read_api import list_holding_review_payloads
from baibai_engine.research.holding_review_builder import validate_holding_review_scalars_from_db
from baibai_engine.research.importer import import_research_records
from baibai_engine.research.store import (
    PacketPublication,
    ResearchConflictError,
    ResearchStoreService,
)
from baibai_engine.screening.run_store import (
    RunStoreConflictError,
    import_screening_runs,
)
from baibai_engine.tasks.importer import import_task_file
from baibai_engine.tasks.service import TaskConflictError, TaskService

ROOT = Path(__file__).parents[1]


def _screening_run(*, as_of: str, run_at: str) -> dict[str, object]:
    return {
        "run_date": as_of,
        "asof_date": as_of,
        "universe_size": 1,
        "filters": {"scope": "all-common-stocks"},
        "generated_by": "migration-test",
        "data_sources": ["fixture"],
        "run_at": run_at,
        "run_id": f"screening-{as_of.replace('-', '')}",
        "candidates": [
            {
                "ticker": "1301",
                "name": "fixture",
                "sector_33": "fixture",
                "metrics": {},
                "evidence_hits": [],
            }
        ],
        "provider_status_lines": [],
        "fallback_lines": [],
    }


def _research_payload(pattern: str) -> dict[str, object]:
    raw = safe_load(next((ROOT / "records/03-thesis").rglob(pattern)).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_research_import_requires_ledger_and_leaves_no_rows(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    with pytest.raises(ResearchConflictError, match="ledger must be imported"):
        import_research_records(
            ROOT / "records/03-thesis",
            ROOT / "records/04-position",
            db_path=db,
            source_root=ROOT,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM research_packet").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM research_review").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM holding_review").fetchone()[0] == 0


def test_runner_canonicalizes_holding_review_sources_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _ledger_result, parity = import_and_check_ledger(
        ROOT / "records/04-position/portfolio-ledger.yaml",
        db_path=db,
    )
    assert parity.matches
    first = import_research_records(
        ROOT / "records/03-thesis",
        ROOT / "records/04-position",
        db_path=db,
        source_root=ROOT,
    )
    second = import_research_records(
        ROOT / "records/03-thesis",
        ROOT / "records/04-position",
        db_path=db,
        source_root=ROOT,
    )

    assert astuple(first) == (2, 0, 2, 0, 1, 0)
    assert astuple(second) == (0, 2, 0, 2, 0, 1)
    payload = list_holding_review_payloads(db)[0]
    document = HoldingReviewDocument.model_validate(payload)
    assert isinstance(document.sources.ledger, CanonicalSource)
    assert document.sources.ledger.entity_id == "portfolio-ledger"
    assert document.sources.ledger.append_head == LedgerStoreService(db).append_head()
    assert isinstance(document.sources.holding_packet, CanonicalSource)
    assert "ref" not in payload["sources"]["holding_packet"]
    assert "sha256" not in payload["sources"]["holding_packet"]
    validate_holding_review_scalars_from_db(document, db_path=db)


def test_ledger_conflict_rolls_back_missing_rows(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    source_path = ROOT / "records/04-position/portfolio-ledger.yaml"
    import_and_check_ledger(source_path, db_path=db)
    service = LedgerStoreService(db)
    source = service.read_document()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE ledger_event SET payload = json_set(payload, '$.amount_yen', 1) "
            "WHERE append_seq = 1"
        )
        connection.execute("DELETE FROM ledger_event WHERE append_seq = 2")

    with pytest.raises(LedgerConflictError, match="conflicts"):
        service.import_document(source)
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("SELECT count(*) FROM ledger_event").fetchone()[0]
            == len(source.events) - 1
        )


def test_task_conflict_rolls_back_new_row(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    source_path = ROOT / "records/05-task/tasks.yaml"
    import_task_file(source_path, db_path=db)
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    raw["tasks"][0]["title"] = "conflict"
    raw["tasks"].append(
        {
            **raw["tasks"][0],
            "task_id": "task-20260719-new-task",
            "title": "new row that must roll back",
        }
    )
    conflict = tmp_path / "tasks.yaml"
    conflict.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(TaskConflictError):
        import_task_file(conflict, db_path=db)
    assert len(TaskService(db).list()) == 8
    with pytest.raises(ValueError, match="unknown task_id"):
        TaskService(db).get("task-20260719-new-task")


def test_screening_conflict_rolls_back_new_run(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    source.mkdir()
    first_path = source / "first.yaml"
    second_path = source / "second.yaml"
    first = _screening_run(as_of="2026-07-07", run_at="2026-07-08T01:00:00+09:00")
    second = _screening_run(as_of="2026-07-08", run_at="2026-07-09T01:00:00+09:00")
    first_path.write_text(yaml.safe_dump(first), encoding="utf-8")
    second_path.write_text(yaml.safe_dump(second), encoding="utf-8")
    db = tmp_path / "app.sqlite"
    import_screening_runs(source, db_path=db)
    second["universe_size"] = 2
    second_path.write_text(yaml.safe_dump(second), encoding="utf-8")
    third_path = source / "third.yaml"
    third_path.write_text(
        yaml.safe_dump(_screening_run(as_of="2026-07-09", run_at="2026-07-10T01:00:00+09:00")),
        encoding="utf-8",
    )

    with pytest.raises(RunStoreConflictError):
        import_screening_runs(source, db_path=db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 2


def test_research_conflict_rolls_back_new_packet(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    import_and_check_ledger(
        ROOT / "records/04-position/portfolio-ledger.yaml",
        db_path=db,
    )
    import_research_records(
        ROOT / "records/03-thesis",
        ROOT / "records/04-position",
        db_path=db,
        source_root=ROOT,
    )
    original = _research_payload("*-4432-decision.yaml")
    changed = json.loads(json.dumps(original))
    changed["judgment"]["strongest_countercase"] += " changed"

    with pytest.raises(ResearchConflictError):
        ResearchStoreService(db).import_publications(
            packets=(
                PacketPublication("packet-new", original),
                PacketPublication("packet-20260714-4432-r1", changed),
            ),
            reviews=(),
            holding_reviews=(),
        )
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM research_packet WHERE packet_id = 'packet-new'"
            ).fetchone()
            is None
        )
