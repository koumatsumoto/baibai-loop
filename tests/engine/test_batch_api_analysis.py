from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_triage import published_review_set

import baibai_engine.batch_api as batch_api
from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.operation.models import OperationPayload
from baibai_engine.operation.service import OperationService
from baibai_engine.read_api.research_triage import research_triage_payloads_for_review_set
from baibai_engine.screening.research_triage import ResearchTriageConflictError

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime(2026, 9, 1, 18, 0, tzinfo=_JST)


class _Reader:
    payload: dict[str, object]

    def __init__(self, _path=None) -> None:
        pass

    def get_review_set(self, _review_set_id: str) -> SimpleNamespace:
        return SimpleNamespace(payload=self.payload)


def _decisions(*, research: bool = True) -> list[dict[str, object]]:
    return [
        {
            "ticker": "2331",
            "verdict": "research" if research else "skip",
            "rationale": "一次開示で収益持続性を確認する"
            if research
            else "追加調査で識別する仮説がない",
            "research_question": "粗利は持続するか" if research else None,
            "key_risk": "顧客集中" if research else None,
        }
    ]


def test_batch_api_publishes_once_and_reuses_same_exact_judgment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)

    first = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=_NOW,
    )
    repeated = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
    )

    assert repeated == first
    assert len(research_triage_payloads_for_review_set(app_db, "review-set-daily")) == 1
    assert first.entries[0].candidate_snapshot.name == "Company 2331"


def test_batch_api_rejects_changed_or_incomplete_ai_decisions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)
    batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=_NOW,
    )

    with pytest.raises(ResearchTriageConflictError, match="already differs"):
        batch_api.publish_daily_research_triage(
            "review-set-daily",
            _decisions(research=False),
            macro_context_id=None,
            app_db_path=app_db,
            published_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
        )
    with pytest.raises(ValueError, match="every Review Set ticker"):
        batch_api.publish_daily_research_triage(
            "review-set-daily",
            [],
            macro_context_id=None,
            app_db_path=app_db,
            published_at=_NOW,
        )
    assert len(research_triage_payloads_for_review_set(app_db, "review-set-daily")) == 1


def test_batch_api_reconciles_one_exact_triage_after_ambiguous_publish_response(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)
    real_service = batch_api.ResearchTriageService

    class AmbiguousService:
        def __init__(self, path) -> None:
            self._service = real_service(path)

        def publish(self, triage, *, review_set):
            self._service.publish(triage, review_set=review_set)
            raise OSError("commit response unavailable")

    monkeypatch.setattr(batch_api, "ResearchTriageService", AmbiguousService)

    triage = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=_NOW,
    )

    assert triage.review_set_id == "review-set-daily"
    assert len(research_triage_payloads_for_review_set(app_db, "review-set-daily")) == 1


def test_batch_api_rejects_reader_identity_mismatch_before_write(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="different-review-set",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)

    with pytest.raises(ValueError, match="different identity"):
        batch_api.publish_daily_research_triage(
            "review-set-daily",
            _decisions(),
            macro_context_id=None,
            app_db_path=app_db,
            published_at=_NOW,
        )
    assert research_triage_payloads_for_review_set(app_db, "review-set-daily") == []


def test_operation_starts_after_research_triage_and_reuses_only_exact_reference(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)
    triage = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=_NOW,
    )

    first = batch_api.ensure_daily_research_operation(
        triage,
        app_db_path=app_db,
        started_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
    )
    repeated = batch_api.ensure_daily_research_operation(
        triage,
        app_db_path=app_db,
        started_at=datetime(2026, 9, 1, 18, 2, tzinfo=_JST),
    )

    assert first is not None
    assert repeated == first
    assert first.payload.canonical_refs == (triage.research_triage_id,)

    service = OperationService(tmp_path / "other.sqlite")
    unrelated = service.start(
        session_kind="capital-allocation",
        as_of=triage.as_of,
        started_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
        payload=OperationPayload(checkpoint="unrelated", next="wait"),
    )
    assert unrelated.as_of == triage.as_of
    with pytest.raises(ValueError, match="active operation already exists"):
        batch_api.ensure_daily_research_operation(
            triage,
            app_db_path=tmp_path / "other.sqlite",
            started_at=datetime(2026, 9, 1, 18, 2, tzinfo=_JST),
        )


def test_all_skip_creates_no_operation(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)
    triage = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(research=False),
        macro_context_id=None,
        app_db_path=app_db,
        published_at=_NOW,
    )

    assert (
        batch_api.ensure_daily_research_operation(
            triage,
            app_db_path=app_db,
            started_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
        )
        is None
    )
    assert OperationService(app_db).active() is None


def test_batch_api_binds_the_macro_context_loaded_before_a_new_head(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)
    first = MacroContextDocument.model_validate(
        macro_context_payload(
            context_id="macro-context-2026-09-01-first",
            as_of="2026-09-01",
            published_at="2026-09-01T10:00:00+09:00",
        )
    )
    MacroContextService(app_db).publish(first, expected_head=None)
    loaded = batch_api.load_daily_analysis_context("review-set-daily", app_db_path=app_db)
    assert loaded.macro_context is not None
    assert loaded.macro_context.context_id == first.context_id

    second = MacroContextDocument.model_validate(
        macro_context_payload(
            context_id="macro-context-2026-09-01-second",
            as_of="2026-09-01",
            published_at="2026-09-01T11:00:00+09:00",
        )
    )
    MacroContextService(app_db).publish(second, expected_head=first.context_id)

    triage = batch_api.publish_daily_research_triage(
        "review-set-daily",
        _decisions(),
        macro_context_id=loaded.macro_context.context_id,
        app_db_path=app_db,
        published_at=_NOW,
    )

    assert triage.macro_context_id == first.context_id


def test_batch_api_rejects_an_invalid_explicit_macro_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db = tmp_path / "app.sqlite"
    initialize_database(app_db)
    _Reader.payload = published_review_set(
        as_of="2026-09-01",
        review_set_id="review-set-daily",
        run_revision_id="run-daily",
        tickers=("2331",),
    )
    monkeypatch.setattr(batch_api, "ScreeningRunReader", _Reader)

    with pytest.raises(ResearchTriageConflictError, match="unknown context_id"):
        batch_api.publish_daily_research_triage(
            "review-set-daily",
            _decisions(),
            macro_context_id="macro-context-missing",
            app_db_path=app_db,
            published_at=_NOW,
        )

    assert research_triage_payloads_for_review_set(app_db, "review-set-daily") == []
