from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.position.drafts import apply_draft, build_event_draft
from baibai_engine.position.importer import import_ledger_file
from baibai_engine.position.ledger import ContributionEvent
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.holding_review_builder import build_holding_review_from_db
from baibai_engine.research.importer import import_research_records
from baibai_engine.research.store import ResearchStoreService

ROOT = Path(__file__).parents[1]
PACKET_ID = "packet-20260714-4432-r1"


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    import_research_records(
        ROOT / "records/03-thesis",
        ROOT / "records/04-position",
        db_path=db,
    )
    import_ledger_file(ROOT / "records/04-position/portfolio-ledger.yaml", db_path=db)
    return db


def test_db_holding_review_build_and_publish_recheck_canonical_revisions(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    document = build_holding_review_from_db(
        db_path=db,
        holding_packet_id=PACKET_ID,
        position_id="4432",
    )

    published = ResearchStoreService(db).publish_holding_review(
        "holding-review-20260714-4432-db-r2",
        PACKET_ID,
        document.model_dump(mode="json"),
        root=ROOT,
    )
    assert published.sources.ledger.append_head == 23  # type: ignore[union-attr]

    stale = build_holding_review_from_db(
        db_path=db,
        holding_packet_id=PACKET_ID,
        position_id="4432",
    )
    ledger = LedgerStoreService(db)
    mutation = build_event_draft(
        ledger,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-after-review-draft",
                "type": "contribution",
                "occurred_at": "2026-07-19T12:00:00+09:00",
                "amount_yen": 1_000,
            }
        ),
    )
    apply_draft(ledger, mutation, human_confirmed=True)

    with pytest.raises(ValueError, match="source changed"):
        ResearchStoreService(db).publish_holding_review(
            "holding-review-20260714-4432-stale",
            PACKET_ID,
            stale.model_dump(mode="json"),
            root=ROOT,
        )
