from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests.helpers.assessment_history import historical_context_payload, seed_history
from tests.helpers.research_triage import published_review_set, research_triage_payload

from baibai_engine.market_context.assessment.models import StoredAssessment
from baibai_engine.research.triage.cli import scaffold_research_triage
from baibai_engine.research.triage.contracts import ResearchTriage
from baibai_engine.research.triage.service import ResearchTriageConflictError, ResearchTriageService
from baibai_engine.screening.discovery.contracts import PublishedReviewSet


def _review_set(*, as_of: str = "2026-07-19") -> dict[str, object]:
    return published_review_set(
        as_of=as_of,
        review_set_id="review-set-macro-binding",
        run_revision_id="runrev-macro-binding",
    )


def _publish_context(path: Path) -> str:
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(path, (document).document, supersedes=None)
    return document.context_id


def _triage(
    *, market_context_assessment_id: str | None, as_of: str = "2026-07-19"
) -> ResearchTriage:
    return ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id=f"research-triage-{as_of.replace('-', '')}-binding",
            review_set_id="review-set-macro-binding",
            run_revision_id="runrev-macro-binding",
            as_of=as_of,
            market_context_assessment_id=market_context_assessment_id,
        )
    )


def test_scaffold_binds_latest_eligible_market_context_assessment(
    tmp_path: Path, monkeypatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    output = tmp_path / "triage.yaml"
    monkeypatch.setattr(
        "baibai_engine.research.triage.cli.ScreeningRunReader.get_review_set",
        lambda *_args: SimpleNamespace(payload=_review_set()),
    )

    assert (
        scaffold_research_triage("review-set-macro-binding", output_path=output, app_db_path=app_db)
        == 0
    )

    draft = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert draft["market_context_assessment_id"] == context_id


def test_scaffold_warns_but_keeps_a_stale_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    output = tmp_path / "triage.yaml"
    monkeypatch.setattr(
        "baibai_engine.research.triage.cli.ScreeningRunReader.get_review_set",
        lambda *_args: SimpleNamespace(payload=_review_set(as_of="2026-09-03")),
    )

    assert (
        scaffold_research_triage("review-set-macro-binding", output_path=output, app_db_path=app_db)
        == 0
    )

    assert (
        yaml.safe_load(output.read_text(encoding="utf-8"))["market_context_assessment_id"]
        == context_id
    )
    assert "warning: latest eligible market context assessment is stale" in capsys.readouterr().err


def test_publish_requires_an_existing_nonfuture_context_when_one_is_named(
    tmp_path: Path,
) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    service = ResearchTriageService(app_db)

    # A saved full-depth report is optional; naming one still validates its identity/cutoff.
    published = service.publish(
        _triage(market_context_assessment_id=None),
        review_set=PublishedReviewSet.model_validate(_review_set()),
    )
    assert published.market_context_assessment_id is None
    with pytest.raises(ResearchTriageConflictError, match="unknown context_id"):
        service.publish(
            _triage(market_context_assessment_id="macro-context-2026-07-19-unknown"),
            review_set=PublishedReviewSet.model_validate(_review_set()),
        )
    with pytest.raises(ResearchTriageConflictError, match="unavailable"):
        service.publish(
            _triage(market_context_assessment_id=context_id, as_of="2026-07-18"),
            review_set=PublishedReviewSet.model_validate(_review_set(as_of="2026-07-18")),
        )


def test_publish_accepts_the_latest_eligible_context(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)

    published = ResearchTriageService(app_db).publish(
        _triage(market_context_assessment_id=context_id),
        review_set=PublishedReviewSet.model_validate(_review_set()),
    )

    assert published.market_context_assessment_id == context_id


def test_publish_accepts_null_when_no_eligible_context_exists(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"

    published = ResearchTriageService(app_db).publish(
        _triage(market_context_assessment_id=None),
        review_set=PublishedReviewSet.model_validate(_review_set()),
    )

    assert published.market_context_assessment_id is None


def test_publish_checks_the_frozen_input_cutoff_before_judgment_publication(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    with pytest.raises(ResearchTriageConflictError, match="unavailable"):
        ResearchTriageService(app_db).publish(
            _triage(market_context_assessment_id=context_id),
            review_set=PublishedReviewSet.model_validate(_review_set()),
            known_at=datetime.fromisoformat("2026-07-19T11:00:00+09:00"),
        )
