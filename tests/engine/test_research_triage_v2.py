from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.research_triage import (
    ResearchTriage,
    ResearchTriageConflictError,
    ResearchTriageService,
)

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime(2026, 7, 22, 12, tzinfo=_JST)


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
    assert published.admissible_research_tickers() == ("2331",)


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
    ("current_as_of", "current_time", "next_as_of", "next_time"),
    [
        (
            "2026-07-19",
            "2026-07-19T10:00:00+09:00",
            "2026-07-20",
            "2026-07-20T10:00:00+09:00",
        ),
        (
            "2026-07-20",
            "2026-07-20T10:00:00+09:00",
            "2026-07-20",
            "2026-07-20T10:00:01+09:00",
        ),
    ],
    ids=["later-as-of", "same-as-of-later-publication"],
)
def test_publish_accepts_monotonic_head_progression(
    tmp_path: Path,
    current_as_of: str,
    current_time: str,
    next_as_of: str,
    next_time: str,
) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    current, current_review_set = _publication(
        as_of=current_as_of,
        published_at=current_time,
        research_triage_id="triage-current",
        expected_prior=None,
    )
    following, following_review_set = _publication(
        as_of=next_as_of,
        published_at=next_time,
        research_triage_id="triage-following",
        expected_prior=current.research_triage_id,
    )
    service = ResearchTriageService(db_path)

    assert (
        service.publish(current, review_set=current_review_set, now=_NOW).research_triage_id
        == current.research_triage_id
    )
    assert (
        service.publish(following, review_set=following_review_set, now=_NOW).research_triage_id
        == following.research_triage_id
    )


def test_first_current_publish_ignores_retired_history_without_rewriting_it(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    historical_payload = {
        "schema_version": 1,
        "research_triage_id": "triage-historical-v1",
        "entries": [],
    }
    historical_bytes = json.dumps(historical_payload, separators=(",", ":"))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO research_triage "
            "(research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "triage-historical-v1",
                "review-set-historical-v1",
                "run-historical-v1",
                "2026-07-18",
                "2026-07-18T10:00:00+09:00",
                historical_bytes,
            ),
        )
    triage, review_set = _publication(
        as_of="2026-07-19",
        published_at="2026-07-19T10:00:00+09:00",
        research_triage_id="triage-first-v3",
        expected_prior=None,
    )

    assert (
        ResearchTriageService(db_path)
        .publish(triage, review_set=review_set, now=_NOW)
        .research_triage_id
        == triage.research_triage_id
    )
    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT payload FROM research_triage WHERE research_triage_id = ?",
            ("triage-historical-v1",),
        ).fetchone()
    assert stored is not None
    assert stored[0] == historical_bytes


def test_same_prior_branch_is_rejected_after_the_first_draft_advances_head(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    current, current_review_set = _publication(
        as_of="2026-07-19",
        published_at="2026-07-19T10:00:00+09:00",
        research_triage_id="triage-current",
        expected_prior=None,
    )
    first, first_review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T10:00:00+09:00",
        research_triage_id="triage-first-branch",
        expected_prior=current.research_triage_id,
    )
    second, second_review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T11:00:00+09:00",
        research_triage_id="triage-second-branch",
        expected_prior=current.research_triage_id,
    )
    service = ResearchTriageService(db_path)
    service.publish(current, review_set=current_review_set, now=_NOW)
    service.publish(first, review_set=first_review_set, now=_NOW)

    with pytest.raises(ResearchTriageConflictError, match="expected prior ID is stale"):
        service.publish(second, review_set=second_review_set, now=_NOW)


def test_same_payload_remains_idempotent_after_a_later_head_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    first, first_review_set = _publication(
        as_of="2026-07-19",
        published_at="2026-07-19T10:00:00+09:00",
        research_triage_id="triage-first",
        expected_prior=None,
    )
    later, later_review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T10:00:00+09:00",
        research_triage_id="triage-later",
        expected_prior=first.research_triage_id,
    )
    service = ResearchTriageService(db_path)
    first_published = service.publish(first, review_set=first_review_set, now=_NOW)
    service.publish(later, review_set=later_review_set, now=_NOW)

    assert service.publish(first, review_set=first_review_set, now=_NOW) == first_published


@pytest.mark.parametrize(
    ("published_at", "now", "message"),
    [
        (
            "2026-07-20T12:00:01+09:00",
            datetime(2026, 7, 20, 12, tzinfo=_JST),
            "published-at must not be future",
        ),
        (
            "2026-07-19T14:59:59+00:00",
            _NOW,
            "JST publication date must not precede as-of",
        ),
    ],
    ids=["future", "JST-date-before-as-of"],
)
def test_publish_rejects_invalid_publication_clock(
    tmp_path: Path, published_at: str, now: datetime, message: str
) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    triage, review_set = _publication(
        as_of="2026-07-20",
        published_at=published_at,
        research_triage_id="triage-invalid-clock",
        expected_prior=None,
    )

    with pytest.raises(ResearchTriageConflictError, match=message):
        ResearchTriageService(db_path).publish(triage, review_set=review_set, now=now)


def test_publish_rejects_a_naive_injected_clock(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    initialize_database(db_path)
    triage, review_set = _publication(
        as_of="2026-07-20",
        published_at="2026-07-20T10:00:00+09:00",
        research_triage_id="triage-naive-clock",
        expected_prior=None,
    )

    with pytest.raises(ResearchTriageConflictError, match="clock must include a timezone"):
        ResearchTriageService(db_path).publish(
            triage,
            review_set=review_set,
            now=datetime(2026, 7, 20, 12),  # noqa: DTZ001 - rejected naive clock fixture
        )


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


def test_judgment_prose_is_validated_without_rewriting_whitespace() -> None:
    rationale = "  業績回復の持続性を\n一次開示で確認する  "
    entry = ResearchTriageEntry.model_validate(research_entry(rationale=rationale))

    assert entry.rationale == rationale


def test_leading_whitespace_does_not_bypass_todo_rejection() -> None:
    with pytest.raises(ValidationError, match="replace scaffold TODO"):
        ResearchTriageEntry.model_validate(research_entry(rationale="   TODO — 判断を書く"))


def test_research_priorities_remain_contiguous() -> None:
    payload = research_triage_payload(
        entries=[research_entry("2331", rank=1), research_entry("0001", rank=3)]
    )

    with pytest.raises(ValidationError, match="priorities must be contiguous"):
        ResearchTriage.model_validate(payload)
