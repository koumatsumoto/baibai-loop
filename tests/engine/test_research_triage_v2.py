from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.helpers.research_triage import (
    published_review_set,
    research_entry,
    research_triage_payload,
    skip_entry,
)

from baibai_engine.appdb.write import initialize_database
from baibai_engine.foundation.research_triage import ResearchTriageEntry
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api.er_calibration_context import ErCalibrationContextArtifact
from baibai_engine.screening.calibration.context import (
    validate_er_distribution_context_payload,
)
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


def _publication(
    *,
    as_of: str,
    published_at: str,
    research_triage_id: str,
    expected_prior: str | None,
) -> tuple[ResearchTriage, PublishedReviewSet]:
    review_set = PublishedReviewSet.model_validate(
        published_review_set(
            as_of=as_of,
            review_set_id=f"review-set-{research_triage_id}",
            run_revision_id=f"run-{research_triage_id}",
            tickers=("2331", "0001"),
        )
    )
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id=research_triage_id,
            review_set_id=review_set.review_set_id,
            run_revision_id=review_set.run_revision_id,
            as_of=as_of,
            published_at=published_at,
            expected_prior_research_triage_id=expected_prior,
            entries=[research_entry("2331", rank=1), skip_entry("0001")],
        )
    )
    return triage, review_set


def test_publish_rejects_an_older_as_of_even_when_prior_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    current, current_review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T10:00:00+09:00",
        research_triage_id="triage-current",
        expected_prior=None,
    )
    stale, stale_review_set = _publication(
        as_of="2026-07-19",
        published_at="2026-07-20T11:00:00+09:00",
        research_triage_id="triage-stale-as-of",
        expected_prior=current.research_triage_id,
    )
    service = ResearchTriageService(db_path)
    service.publish(current, review_set=current_review_set)

    with pytest.raises(ResearchTriageConflictError, match="as-of must not move backward"):
        service.publish(stale, review_set=stale_review_set)


@pytest.mark.parametrize(
    "published_at",
    ["2026-07-20T10:00:00+09:00", "2026-07-20T09:59:59+09:00"],
    ids=["equal", "older"],
)
def test_publish_rejects_non_increasing_published_at(tmp_path: Path, published_at: str) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    current, current_review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T10:00:00+09:00",
        research_triage_id="triage-current",
        expected_prior=None,
    )
    stale, stale_review_set = _publication(
        as_of="2026-07-20",
        published_at=published_at,
        research_triage_id=f"triage-{published_at}",
        expected_prior=current.research_triage_id,
    )
    service = ResearchTriageService(db_path)
    service.publish(current, review_set=current_review_set)

    with pytest.raises(ResearchTriageConflictError, match="published-at must advance"):
        service.publish(stale, review_set=stale_review_set)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rationale", " "),
        ("research_question", "\n\t"),
        ("key_risk", "   "),
    ],
)
def test_research_triage_entry_rejects_blank_judgment_prose(field: str, value: str) -> None:
    payload = research_entry()
    payload[field] = value

    with pytest.raises(ValidationError, match=f"{field} must contain non-whitespace text"):
        ResearchTriageEntry.model_validate(payload)


def test_skip_entry_rejects_blank_rationale() -> None:
    with pytest.raises(ValidationError, match="rationale must contain non-whitespace text"):
        ResearchTriageEntry.model_validate(skip_entry("2331", reason=" \n\t"))


@pytest.mark.parametrize("location", ["root", "band", "stats"])
def test_er_context_producer_and_typed_reader_both_reject_unknown_fields(location: str) -> None:
    artifact_path = (
        Path(__file__).resolve().parents[2] / "reports/published/er-level-calibration-latest.yaml"
    )
    payload = safe_load(artifact_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    mutated = deepcopy(payload)
    if location == "root":
        mutated["unknown"] = "value"
    else:
        horizons = mutated["horizons"]
        assert isinstance(horizons, list)
        first_horizon = horizons[0]
        assert isinstance(first_horizon, dict)
        bands = first_horizon["bands"]
        assert isinstance(bands, list)
        first_band = bands[0]
        assert isinstance(first_band, dict)
        if location == "band":
            first_band["unknown"] = "value"
        else:
            bases = first_band["bases"]
            assert isinstance(bases, list)
            first_basis = bases[0]
            assert isinstance(first_basis, dict)
            stats = first_basis["ticker_equal"]
            assert isinstance(stats, dict)
            stats["unknown"] = "value"

    assert validate_er_distribution_context_payload(mutated) is False
    with pytest.raises(ValidationError):
        ErCalibrationContextArtifact.model_validate(mutated)
