from __future__ import annotations

from datetime import date, datetime

from tests.helpers.research_triage import research_triage_payload, skip_entry

from baibai_web.readmodel.builders import build_screening
from baibai_web.sources.db_sources import DbCandidatesSource
from baibai_web.sources.types import CandidatesRun


def _run() -> CandidatesRun:
    return CandidatesRun(
        run_id="screening-20260828",
        run_date=date(2026, 8, 28),
        asof_date=date(2026, 8, 28),
        run_at=datetime.fromisoformat("2026-08-28T18:00:00+09:00"),
        universe_size=0,
        run_revision_id="run-current",
        rules_ref=None,
        screening_rules_hash="rules-current",
        er_model_version="expected-return-v1",
        rows=(),
    )


def _review_set() -> dict[str, object]:
    return {
        "review_set_id": "review-set-current",
        "run_revision_id": "run-current",
        "created_at": "2026-08-28T18:01:00+09:00",
        "payload": {"entries": []},
    }


def _assessment(assessment_id: str, research_triage_id: str) -> dict[str, object]:
    return {
        "capital_allocation_assessment_id": assessment_id,
        "as_of": "2026-08-28",
        "published_at": "2026-08-28T18:03:00+09:00",
        "result": "no_allocation",
        "headline": "要求利回り未達のため配分しない",
        "research_triage_id": research_triage_id,
        "alternatives": [],
    }


def _source(tmp_path, mocker) -> DbCandidatesSource:
    source = DbCandidatesSource(tmp_path / "runs.sqlite", tmp_path / "app.sqlite")
    mocker.patch.object(source, "latest_run", return_value=_run())
    mocker.patch.object(source, "review_sets", return_value=[_review_set()])
    return source


def _dependencies(mocker):
    ledger = mocker.Mock()
    ledger.exists.return_value = False
    research = mocker.Mock()
    research.revisions.return_value = []
    return ledger, research


def test_screening_omits_judgments_from_another_review_set_cycle(tmp_path, mocker) -> None:
    source = _source(tmp_path, mocker)
    old_triage = research_triage_payload(
        research_triage_id="triage-old",
        review_set_id="review-set-old",
        run_revision_id="run-old",
        entries=[skip_entry("3836")],
    )
    mocker.patch.object(source, "research_triages", return_value=[old_triage])
    mocker.patch.object(
        source, "assessments", return_value=[_assessment("assessment-old", "triage-old")]
    )
    ledger, research = _dependencies(mocker)

    view = build_screening(source, ledger, research)

    assert [item.review_set_id for item in view.review_sets] == ["review-set-current"]
    assert view.research_triages == []
    assert view.capital_allocation_assessments == []


def test_screening_projects_only_the_assessment_bound_to_the_current_triage(
    tmp_path, mocker
) -> None:
    source = _source(tmp_path, mocker)
    current_triage = research_triage_payload(
        research_triage_id="triage-current",
        review_set_id="review-set-current",
        run_revision_id="run-current",
        entries=[skip_entry("6419")],
    )
    mocker.patch.object(source, "research_triages", return_value=[current_triage])
    mocker.patch.object(
        source,
        "assessments",
        return_value=[
            _assessment("assessment-old", "triage-old"),
            _assessment("assessment-current", "triage-current"),
        ],
    )
    ledger, research = _dependencies(mocker)

    view = build_screening(source, ledger, research)

    assert [item.research_triage_id for item in view.research_triages] == ["triage-current"]
    assert [
        item.capital_allocation_assessment_id for item in view.capital_allocation_assessments
    ] == ["assessment-current"]
