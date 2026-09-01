from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.research_triage import (
    published_review_set,
    research_entry,
    research_triage_payload,
    skip_entry,
)

from baibai_engine.appdb.write import initialize_database
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.research_triage import (
    ResearchTriage,
    ResearchTriageConflictError,
    ResearchTriageService,
)


def _contracts() -> tuple[ResearchTriage, PublishedReviewSet]:
    review_set = PublishedReviewSet.model_validate(published_review_set(tickers=("2331", "0001")))
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            review_set_id=review_set.review_set_id,
            entries=[research_entry("2331", rank=1), skip_entry("0001")],
        )
    )
    return triage, review_set


def test_publish_canonicalizes_snapshot_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    triage, review_set = _contracts()
    tampered = triage.model_copy(
        update={
            "entries": (
                triage.entries[0].model_copy(
                    update={
                        "candidate_snapshot": triage.entries[0].candidate_snapshot.model_copy(
                            update={"name": "tampered"}
                        )
                    }
                ),
                triage.entries[1],
            )
        }
    )
    service = ResearchTriageService(db_path)

    published = service.publish(tampered, review_set=review_set)
    repeated = service.publish(tampered, review_set=review_set)

    assert published == repeated
    assert published.entries[0].candidate_snapshot.name == "Company 2331"
    assert published.researchable_tickers() == ("2331",)


def test_same_id_with_different_human_judgment_conflicts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    triage, review_set = _contracts()
    service = ResearchTriageService(db_path)
    service.publish(triage, review_set=review_set)
    changed = triage.model_copy(
        update={
            "entries": (
                triage.entries[0].model_copy(update={"rationale": "different rationale"}),
                triage.entries[1],
            )
        }
    )

    with pytest.raises(ResearchTriageConflictError, match="identity already differs"):
        service.publish(changed, review_set=review_set)


def test_stale_prior_and_review_set_identity_mismatch_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    first, review_set = _contracts()
    service = ResearchTriageService(db_path)
    service.publish(first, review_set=review_set)
    second = first.model_copy(update={"research_triage_id": "research-triage-next"})

    with pytest.raises(ResearchTriageConflictError, match="expected prior ID is stale"):
        service.publish(second, review_set=review_set)

    mismatched = second.model_copy(
        update={
            "expected_prior_research_triage_id": first.research_triage_id,
            "screening_rules_hash": "f" * 64,
        }
    )
    with pytest.raises(ResearchTriageConflictError, match="rules identity differs"):
        service.publish(mismatched, review_set=review_set)
