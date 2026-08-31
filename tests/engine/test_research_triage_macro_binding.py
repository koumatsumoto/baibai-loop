from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_triage import research_triage_payload

from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.screening.research_triage import (
    ResearchTriage,
    ResearchTriageConflictError,
    ResearchTriageService,
)
from baibai_engine.screening.research_triage_cli import scaffold_research_triage


def _review_set(*, as_of: str = "2026-07-19") -> dict[str, object]:
    return {
        "review_set_id": "review-set-macro-binding",
        "run_revision_id": "runrev-macro-binding",
        "as_of": as_of,
        "review_basis": {"judged_through_research_triage_id": None},
        "entries": [
            {
                "ticker": "2331",
                "review_position": 1,
                "nominations": [{"valuation_approach_id": "current-earnings-power"}],
                "support_count": 1,
                "analysis": {"expected_return": {"er_annual": 0.1}, "data_quality": {}},
            }
        ],
    }


def _publish_context(path: Path) -> str:
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(path).publish(document, expected_head=None)
    return document.context_id


def _triage(*, macro_context_id: str | None, as_of: str = "2026-07-19") -> ResearchTriage:
    return ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id=f"research-triage-{as_of.replace('-', '')}-binding",
            review_set_id="review-set-macro-binding",
            run_revision_id="runrev-macro-binding",
            as_of=as_of,
            macro_context_id=macro_context_id,
        )
    )


def test_scaffold_binds_latest_eligible_macro_context(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    source = tmp_path / "review-set.yaml"
    output = tmp_path / "triage.yaml"
    source.write_text(yaml.safe_dump(_review_set(), sort_keys=False), encoding="utf-8")

    assert scaffold_research_triage(source, output_path=output, app_db_path=app_db) == 0

    draft = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert draft["macro_context_id"] == context_id


def test_scaffold_warns_but_keeps_a_stale_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    source = tmp_path / "review-set.yaml"
    output = tmp_path / "triage.yaml"
    source.write_text(
        yaml.safe_dump(_review_set(as_of="2026-09-03"), sort_keys=False),
        encoding="utf-8",
    )

    assert scaffold_research_triage(source, output_path=output, app_db_path=app_db) == 0

    assert yaml.safe_load(output.read_text(encoding="utf-8"))["macro_context_id"] == context_id
    assert "warning: latest eligible macro context is stale" in capsys.readouterr().err


def test_publish_requires_an_existing_nonfuture_context_when_one_is_named(
    tmp_path: Path,
) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)
    service = ResearchTriageService(app_db)

    with pytest.raises(ResearchTriageConflictError, match="must be bound"):
        service.publish(_triage(macro_context_id=None), review_set=_review_set())
    with pytest.raises(ResearchTriageConflictError, match="unknown context_id"):
        service.publish(
            _triage(macro_context_id="macro-context-2026-07-19-unknown"), review_set=_review_set()
        )
    with pytest.raises(ResearchTriageConflictError, match="future macro context"):
        service.publish(
            _triage(macro_context_id=context_id, as_of="2026-07-18"),
            review_set=_review_set(as_of="2026-07-18"),
        )


def test_publish_accepts_the_latest_eligible_context(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    context_id = _publish_context(app_db)

    published = ResearchTriageService(app_db).publish(
        _triage(macro_context_id=context_id),
        review_set=_review_set(),
    )

    assert published.macro_context_id == context_id


def test_publish_accepts_null_when_no_eligible_context_exists(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"

    published = ResearchTriageService(app_db).publish(
        _triage(macro_context_id=None),
        review_set=_review_set(),
    )

    assert published.macro_context_id is None
