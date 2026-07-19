from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.position.drafts import apply_draft, build_event_draft
from baibai_engine.position.importer import import_ledger_file
from baibai_engine.position.ledger import ContributionEvent
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "records/04-position/portfolio-ledger.yaml"


def test_event_draft_requires_confirmation_and_rejects_stale_apply(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    import_ledger_file(LEDGER, db_path=db)
    service = LedgerStoreService(db)
    first = build_event_draft(
        service,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-test-1",
                "type": "contribution",
                "occurred_at": "2026-07-19T12:00:00+09:00",
                "amount_yen": 10_000,
            }
        ),
    )
    second = build_event_draft(
        service,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-test-2",
                "type": "contribution",
                "occurred_at": "2026-07-19T12:01:00+09:00",
                "amount_yen": 20_000,
            }
        ),
    )

    with pytest.raises(ValueError, match="human confirmation"):
        apply_draft(service, first, human_confirmed=False)
    applied = apply_draft(service, first, human_confirmed=True)
    assert applied.event_ids == ("human-contribution-test-1",)
    with pytest.raises(LedgerConflictError, match="stale"):
        apply_draft(service, second, human_confirmed=True)
