from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.drafts import apply_draft, build_event_draft
from baibai_engine.position.ledger import ContributionEvent, load_portfolio_ledger
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.holding_review_builder import build_holding_review_from_db
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    independent_review_hash,
    thesis_core_hash,
)
from tests.helpers.db_seed import seed_ledger

FIXTURES = Path(__file__).parent / "fixtures"
THESIS_ID = "thesis-20260714-2331-r1"


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    ledger = load_portfolio_ledger(FIXTURES / "portfolio-ledger/representative.yaml")
    seed_ledger(
        db,
        ledger.model_copy(
            update={
                "as_of": datetime.fromisoformat("2026-07-03T10:00:00+09:00"),
                "market_prices": tuple(
                    price.model_copy(
                        update={
                            "price_yen": Decimal("1032"),
                            "observed_at": datetime.fromisoformat("2026-07-03T08:30:00+09:00"),
                            "source_kind": "licensed_dataset",
                            "price_basis": "unadjusted_close",
                        }
                    )
                    for price in ledger.market_prices
                ),
            }
        ),
    )
    thesis = safe_load((FIXTURES / "thesis/2331-decision.yaml").read_text(encoding="utf-8"))
    review = safe_load((FIXTURES / "thesis/2331-decision-review.yaml").read_text(encoding="utf-8"))
    assert isinstance(thesis, dict)
    assert isinstance(review, dict)
    input_snapshot = thesis["input_snapshot"]
    assert isinstance(input_snapshot, dict)
    facts = input_snapshot["facts"]
    assert isinstance(facts, list)
    assert isinstance(facts[0], dict)
    facts[0]["price_basis"] = "last_close_unadjusted"
    core_hash = thesis_core_hash(ThesisDocument.model_validate(thesis))
    review["reviewed_thesis_sha256"] = core_hash
    review_hash = independent_review_hash(IndependentReview.model_validate(review))
    evidence_override = thesis["human_evidence_override"]
    assert isinstance(evidence_override, dict)
    evidence_override["proposal_sha256"] = core_hash
    evidence_override["review_sha256"] = review_hash
    ResearchStoreService(db).publish_thesis_with_review(THESIS_ID, thesis, review)
    return db


def test_db_holding_review_build_and_publish_recheck_canonical_revisions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _database(tmp_path)
    document = build_holding_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
    )

    published = ResearchStoreService(db).publish_holding_review(
        "holding-review-20260714-2331-db-r2",
        THESIS_ID,
        document.model_dump(mode="json"),
    )
    assert published.sources.ledger.append_head == LedgerStoreService(db).append_head()

    stale = build_holding_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
    )
    ledger = LedgerStoreService(db)
    mutation = build_event_draft(
        ledger,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-after-review-draft",
                "type": "contribution",
                "occurred_at": "2026-07-04T12:00:00+09:00",
                "amount_yen": 1_000,
            }
        ),
    )
    apply_draft(ledger, mutation, human_confirmed=True)

    with pytest.raises(ValueError, match="source changed"):
        ResearchStoreService(db).publish_holding_review(
            "holding-review-20260714-2331-stale",
            THESIS_ID,
            stale.model_dump(mode="json"),
        )

    draft = tmp_path / "stale-holding-review.yaml"
    draft.write_text(
        yaml.safe_dump(stale.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    assert position_main(["holding-review", "--db", str(db), "--input", str(draft)]) == 2
    assert "source changed" in capsys.readouterr().err
