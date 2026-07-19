from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.position.drafts import apply_draft, build_event_draft
from baibai_engine.position.ledger import ContributionEvent, load_portfolio_ledger
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService
from tests.helpers.db_seed import seed_ledger

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"


def test_event_draft_requires_confirmation_and_rejects_stale_apply(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(
        db,
        source.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in source.market_prices
                )
            }
        ),
    )
    service = LedgerStoreService(db)
    first = build_event_draft(
        service,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-test-1",
                "type": "contribution",
                "occurred_at": "2026-07-18T09:00:00+09:00",
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
                "occurred_at": "2026-07-18T09:01:00+09:00",
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
