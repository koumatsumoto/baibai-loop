"""部分保存、再訪、固定入力を有限fixtureで確認する。実原典の真偽は認定しない。"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.research_selection import historical_operation

from baibai_engine.investment_judgment.company_research.context import AvailableResearch, Unreadable
from baibai_engine.investment_judgment.company_research.document import (
    ResearchDocument,
    ResearchDraft,
    scaffold_research,
)
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchConflictError,
    ResearchReadError,
    ResearchStoreService,
    load_research,
    require_research_ref,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionV4,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
AS_OF = NOW.date()


def draft(identity="r1", *, ticker="1234", as_of="2026-10-14", prior=None, evidence=False):
    document = {
        "ticker": ticker,
        "as_of": as_of,
        "investigated_scope": "契約更新の公開資料を検索したが条件を確認できず",
        "unresolved_questions": ["次回開示で更新条件と借換期限を確認する"],
    }
    if evidence:
        document.update(
            sources=[
                {
                    "source_id": "ir",
                    "ticker": ticker,
                    "source_tier": "primary",
                    "ref": "https://example.test/ir",
                    "retrieved_at": "2026-10-01T16:00:00+09:00",
                    "as_of": "2026-09-30",
                    "used_for": "借換と顧客依存",
                }
            ],
            facts=[
                {
                    "fact_id": "debt",
                    "fact_kind": "other",
                    "value": "返済期限は年度末",
                    "unit": "text",
                    "as_of": "2026-09-30",
                    "source_ids": ["ir"],
                }
            ],
            hypotheses=[
                {
                    "statement": "顧客依存が小さい",
                    "origin": "research",
                    "result": "refuted",
                    "reason": "最大顧客の比率が高い",
                    "source_ids": ["ir"],
                }
            ],
        )
    return ResearchDraft.model_validate(
        {
            "research_id": identity,
            "prior_research_ref": None if prior is None else prior.model_dump(mode="json"),
            "document": document,
        }
    )


def read(path, ticker="1234", *, as_of=AS_OF, known_at=NOW):
    return read_company_contexts(path, tickers=(ticker,), as_of=as_of, known_at=known_at)[ticker]


def test_partial_search_failure_save_and_exact_read_has_no_valuation_or_review(tmp_path):
    path = tmp_path / "app"
    saved = ResearchStoreService(path, clock=lambda: NOW).save(draft())
    with closing(connect_read_only(path)) as connection:
        assert require_research_ref(connection, saved.reference) == saved
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_session").fetchone()[0] == 0
        with pytest.raises(ResearchReadError, match="reference differs"):
            require_research_ref(
                connection, saved.reference.model_copy(update={"payload_sha256": "a" * 64})
            )
    assert saved.document.sources == ()
    assert saved.document.hypotheses == ()
    assert read(path).research.role == "unreviewed_research"
    with pytest.raises(ValueError, match="requires a fact"):
        ResearchDocument(ticker="1234", as_of=NOW.date(), investigated_scope="検索のみ")
    with pytest.raises(ValueError, match="Extra inputs"):
        ResearchDraft.model_validate(
            {**draft().model_dump(mode="json"), "recorded_at": NOW.isoformat()}
        )


def test_snapshot_scaffold_retry_and_stale_prior_are_distinct(tmp_path):
    path = tmp_path / "app"
    service = ResearchStoreService(path, clock=lambda: NOW)
    original_draft = draft(evidence=True)
    r1 = service.save(original_draft)
    assert (
        ResearchStoreService(path, clock=lambda: NOW + timedelta(days=1)).save(original_draft) == r1
    )
    copied = scaffold_research(research_id="r2", ticker="1234", as_of=NOW.date(), prior=r1)
    assert copied["document"]["sources"] == r1.document.model_dump(mode="json")["sources"]
    assert copied["document"]["facts"] == r1.document.model_dump(mode="json")["facts"]
    assert copied["document"]["hypotheses"] == r1.document.model_dump(mode="json")["hypotheses"]
    assert copied["document"]["investigated_scope"] == ""
    copied["document"]["investigated_scope"] = (
        "新製品のみ追加検索。借換と顧客依存は再確認していない"
    )
    r2 = ResearchStoreService(path, clock=lambda: NOW + timedelta(microseconds=1)).save(
        ResearchDraft.model_validate(copied)
    )
    assert r2.document.unresolved_questions == r1.document.unresolved_questions
    assert read(path, known_at=NOW + timedelta(seconds=1)).research.record == r2
    with pytest.raises(ResearchConflictError, match="latest"):
        service.save(draft("parallel", prior=r1.reference))
    with pytest.raises(ResearchConflictError, match="immutable"):
        service.save(draft("r1"))
    # A late retry is still the original immutable snapshot, not a new head.
    assert service.save(original_draft) == r1
    with closing(connect_read_only(path)) as connection:
        assert load_research(connection, "r1") == r1


@pytest.mark.parametrize(
    "change",
    [
        "source_duplicate",
        "fact_duplicate",
        "unknown_ref",
        "wrong_ticker",
        "future_source",
        "future_fact",
    ],
)
def test_evidence_reference_and_date_integrity(change):
    raw = draft(evidence=True).model_dump(mode="json")
    document = raw["document"]
    if change == "source_duplicate":
        document["sources"] *= 2
    elif change == "fact_duplicate":
        document["facts"] *= 2
    elif change == "unknown_ref":
        document["facts"][0]["source_ids"] = ["missing"]
    elif change == "wrong_ticker":
        document["sources"][0]["ticker"] = "5678"
    elif change == "future_source":
        document["sources"][0]["as_of"] = "2026-10-15"
    else:
        document["facts"][0]["as_of"] = "2026-10-15"
    with pytest.raises(ValueError, match=r"source|fact"):
        ResearchDraft.model_validate(raw)


def test_latest_uses_real_instant_two_cutoffs_and_monotonic_revision_order(tmp_path):
    path = tmp_path / "app"
    r1 = ResearchStoreService(path, clock=lambda: NOW).save(draft(as_of="2026-10-13"))
    offset_now = datetime.fromisoformat("2026-10-14T09:00:00.000001+00:00")
    r2 = ResearchStoreService(path, clock=lambda: offset_now).save(draft("r2", prior=r1.reference))
    assert read(path).research.record == r1
    assert read(path, known_at=offset_now).research.record == r2
    assert (
        read(path, as_of=NOW.date() - timedelta(days=1), known_at=offset_now).research.record == r1
    )
    with pytest.raises(ResearchConflictError, match="advance"):
        ResearchStoreService(path, clock=lambda: offset_now).save(draft("r0", prior=r2.reference))
    with pytest.raises(ResearchConflictError, match="advance"):
        ResearchStoreService(path, clock=lambda: offset_now + timedelta(seconds=1)).save(
            draft("r3", prior=r2.reference, as_of="2026-10-13")
        )
    with pytest.raises(ResearchConflictError, match="advance"):
        ResearchStoreService(path, clock=lambda: NOW).save(draft("r3", prior=r2.reference))
    with pytest.raises(ValueError, match="timezone"):
        read(path, known_at=NOW.replace(tzinfo=None))


def test_corrupt_latest_is_isolated_but_broken_database_is_not_absence(tmp_path):
    path = tmp_path / "app"
    service = ResearchStoreService(path, clock=lambda: NOW)
    r1 = service.save(draft())
    service.save(draft("r2", prior=r1.reference))
    service.save(draft("other", ticker="5678"))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER company_research_immutable")
        connection.execute(
            "UPDATE company_research SET payload=json_set(payload, '$.schema_version', 999) WHERE research_id='r2'"
        )
    results = read_company_contexts(
        path, tickers=("1234", "5678", "9999"), as_of=AS_OF, known_at=NOW
    )
    assert isinstance(results["1234"].research, Unreadable)
    assert results["1234"].research.record_id == "r2"
    assert isinstance(results["5678"].research, AvailableResearch)
    assert results["9999"].research.status == "not_found"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=25")
    with pytest.raises(RuntimeError, match="schema"):
        read(path)


def test_catalog_includes_assessment_only_and_revisits_noncandidate(tmp_path):
    path, runs = tmp_path / "app", tmp_path / "runs"
    document, review = pair_payload(ticker="5678")
    pair = AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    ResearchStoreService(path, clock=lambda: NOW).save(draft())
    first = research_catalog(path, as_of=AS_OF, known_at=NOW, limit=1)
    second = research_catalog(
        path, as_of=AS_OF, known_at=NOW, after_ticker=first["next_after_ticker"], limit=1
    )
    assert [first["items"][0]["ticker"], second["items"][0]["ticker"]] == ["1234", "5678"]
    assert second["next_after_ticker"] is None
    historical = second["items"][0]
    assert historical["research"]["status"] == "not_found"
    assert historical["assessment"]["reference"]["core_sha256"] == pair.core_sha256
    assert "not_current_readiness" in historical["assessment"]["role"]
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=())
    context = load_generated_selection_context(
        request=selected.request,
        generation_id=selected.inputs.generation.generation_id,
        screening_selection_id=selected.inputs.screening_selection_id,
        research_as_of=NOW.date(),
        known_at=NOW,
        runs_path=runs,
        db_path=path,
        comparison_tickers=("5678",),
    )
    assert context["candidate_tickers"] == []
    assert context["saved_company_context"]["5678"]["assessment"] == historical["assessment"]
    assert context["generation_id"] == selected.inputs.generation.generation_id
    with closing(connect_read_only(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM company_research").fetchone()[0] == 1
    # A partial new Research must not hide an existing Assessment conclusion.
    ResearchStoreService(path, clock=lambda: NOW).save(draft("partial", ticker="5678"))
    context = read(path, "5678")
    assert context.research.status == context.assessment.status == "available"
    assert context.assessment.reference.core_sha256 == pair.core_sha256


def test_selection_v4_freezes_only_selected_context_and_resume_never_reads_latest(
    tmp_path, monkeypatch
):
    path, runs = tmp_path / "app", tmp_path / "runs"
    r1 = ResearchStoreService(path, clock=lambda: NOW - timedelta(seconds=1)).save(
        draft(evidence=True)
    )
    ResearchStoreService(path, clock=lambda: NOW).save(draft("unselected", ticker="5678"))
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=path,
        workspace=tmp_path / "workspace",
        runs_path=runs,
        now=NOW,
    )
    operation = OperationService(path).get(prepared.operation_id)
    saved = selection_binding(operation.payload)
    assert isinstance(saved, GeneratedResearchSelectionV4)
    assert set(saved.inputs.saved_company_context) == {"1234"}
    assert saved.inputs.saved_company_context["1234"].research.record == r1
    before = (tmp_path / "workspace/domain-inputs.yaml").read_bytes()
    digest = selection_sha256(saved)
    ResearchStoreService(path, clock=lambda: NOW + timedelta(seconds=1)).save(
        draft("r2", prior=r1.reference)
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.company_research.query.company_context",
        lambda *a, **kw: pytest.fail("resume must not reread current context"),
    )
    resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=path,
        workspace=tmp_path / "workspace",
        now=NOW + timedelta(seconds=2),
    )
    assert (tmp_path / "workspace/domain-inputs.yaml").read_bytes() == before
    assert (
        selection_sha256(
            selection_binding(OperationService(path).get(prepared.operation_id).payload)
        )
        == digest
    )
    raw = saved.model_dump(mode="json")
    raw["inputs"]["saved_company_context"].clear()
    with pytest.raises(ValueError, match="exactly cover"):
        parse_selection(raw)


def test_legacy_selection_shapes_and_assessment_v1_identity_are_unchanged(tmp_path):
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    legacy = json.loads((fixtures / "domain-assessment-proposal-synthetic.json").read_text())
    v3 = json.loads((fixtures / "research/selection-v3-retained.json").read_text())
    v2 = legacy["research_selection"]
    scope = legacy["investment_proposal"]["research_scope"]
    publication = legacy["assessment_publication"]
    path = tmp_path / "app"
    service = AssessmentStoreService(path, clock=lambda: NOW)
    pair = service.publish_reviewed_assessment(
        publication["assessment_id"], publication["document"], publication["review"]
    )
    ResearchStoreService(path, clock=lambda: NOW).save(draft())
    assert (
        service.publish_reviewed_assessment(
            publication["assessment_id"], publication["document"], publication["review"]
        )
        == pair
    )
    assert pair.document.model_dump(mode="json") == publication["document"]
    assert (
        assessment_core_hash(StockAssessmentDocument.model_validate(publication["document"]))
        == scope["outcomes"][0]["assessment_ref"]["core_sha256"]
    )
    for raw, digest in ((v2, scope["selection_sha256"]), (v3["selection"], v3["sha256"])):
        assert parse_selection(raw).model_dump(mode="json") == raw
        assert selection_sha256(parse_selection(raw)) == digest
    # Old V3 is resume-only, and materialization uses retained bytes without a model store.
    retained = parse_selection(v3["selection"])
    with pytest.raises(ValueError, match="resume-only"):
        OperationService(path).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(checkpoint="new", artifacts=(v3["selection"],)),
        )
    operation = historical_operation(retained)
    seed_historical_operation(path, operation)
    resumed = resume_selected_workspace(
        operation_id=operation.operation_id,
        db_path=path,
        workspace=tmp_path / "legacy-workspace",
        now=NOW,
    )
    assert resumed.selection_sha256 == v3["sha256"]


def test_unwritten_store_is_absence_but_missing_current_table_is_not(tmp_path):
    path = tmp_path / "app"
    with closing(sqlite3.connect(path)):
        pass
    assert read(path).research.status == "not_found"
    assert research_catalog(path, as_of=AS_OF, known_at=NOW)["items"] == []
    ResearchStoreService(path, clock=lambda: NOW).save(draft())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE company_research")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        read(path)
