"""Contracts for the pre-deletion migration runner only."""

from __future__ import annotations

from dataclasses import astuple
from pathlib import Path

from baibai_engine.position.holding_review import CanonicalSource, HoldingReviewDocument
from baibai_engine.position.importer import import_and_check_ledger
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api import list_holding_review_payloads
from baibai_engine.research.holding_review_builder import validate_holding_review_scalars_from_db
from baibai_engine.research.importer import import_research_records

ROOT = Path(__file__).parents[1]


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
