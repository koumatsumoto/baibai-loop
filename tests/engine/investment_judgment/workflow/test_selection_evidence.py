"""現行schemaの合成保存状態で、原公開入力と運用入力を区別する。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
import yaml
from tests.helpers import generated_selection as generation_helpers
from tests.helpers import proposal as proposal_helpers
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tools.financials_identity_workspace import prepare_workspace

from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    load_latest_investment_proposal,
    proposal_records,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation import service as operation_service
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_evidence import read_selection_evidence
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationSubject
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CandidateCard,
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only

AT = PUBLISHED + timedelta(days=1)
WRONG = "investment-proposal-00000000-0000-4000-8000-000000000004"
OLD = "company_valuation"
NEW = "company_financials"


def reference(proposal):
    return ProposalReference(
        proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
    )


def evidence_case(tmp_path, monkeypatch, *, generation_version=1, selection_version=4, damage=None):
    """署名は旧入力に対して先に作る。現schemaへ小さな既知の保存状態をseedする。"""
    if generation_version == 2:
        generation_type = generation_helpers.ModelGeneration

        def generation(**kwargs):
            return generation_type(
                **kwargs,
                schema_version=2,
                subject_plan={
                    "1234": GenerationSubject(scope="stock", model_ids=(OLD, "company_growth")),
                    "market": GenerationSubject(scope="market", model_ids=("market_context",)),
                },
            )

        monkeypatch.setattr(generation_helpers, "ModelGeneration", generation)

    def seed(**kwargs):
        growth = CompanyGrowth(
            ticker="1234",
            status="unavailable",
            requested_start=None,
            requested_end=kwargs["now"].date(),
        )
        kwargs["models"] = (
            GeneratedModel(
                model_id=OLD,
                subject="1234",
                calculation_revision="original-calculation",
                payload={"amount": "123456789.000", "decimals": "-3", "unknown": None},
            ),
            GeneratedModel(
                model_id="company_growth",
                subject="1234",
                calculation_revision="growth-v1",
                payload=growth.model_dump(mode="json"),
            ),
        )
        kwargs["screening_result"] = EconomicPathSelection(
            EconomicPathPolicy(),
            1,
            (
                CandidateCard(
                    ticker="1234",
                    name="架空企業",
                    sector=None,
                    rank=1,
                    priority=0.5,
                    strongest_path="earning_power",
                    paths=(),
                    context={},
                    model_revisions={OLD: "original-calculation"},
                    not_generated=("external_assessment",),
                ),
            ),
        )
        selection = seed_generated_selection(**kwargs)
        if selection_version == 3:
            raw = selection.model_dump(mode="json")
            raw["schema_version"] = 3
            del raw["inputs"]["saved_company_context"]
            return GeneratedResearchSelection.model_validate(raw)
        return selection

    monkeypatch.setattr(proposal_helpers, "seed_generated_selection", seed)
    # 旧V3の開始だけをfixture化し、署名を作る前に通常gateへ戻す。
    with monkeypatch.context() as historical_start:
        if selection_version == 3:
            historical_start.setattr(
                operation_service, "GeneratedResearchSelectionV4", GeneratedResearchSelection
            )
        case = backend_case(tmp_path)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    original = service.publish(reviewed_proposal(case))
    wrong = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=WRONG))
    case.operations.record_proposal(
        case.operation.operation_id, wrong.proposal_id, now=PUBLISHED + timedelta(minutes=2)
    )
    records = ProposalRecordService(case.app, clock=lambda: PUBLISHED + timedelta(hours=1))
    records.correct(
        ProposalCorrectionDraft(
            correction_id="synthetic-correction",
            erroneous_ref=reference(wrong),
            replacement_ref=reference(original),
            reason="架空の担当者の誤記録を訂正する",
        ),
        expected_latest_ref=reference(wrong),
    )
    records.report(
        HumanProposalReportDraft(
            report_id="synthetic-report",
            proposal_ref=reference(original),
            outcome="not_purchased",
            statement="今回は買わなかった",
            reported_at=PUBLISHED + timedelta(minutes=10),
            reason_quote="様子を見る",
            context_summary="架空の文脈",
        )
    )
    published = canonical_json(case.selected.model_dump(mode="json"))
    raw = case.selected.model_dump(mode="json")
    # 汎用変換や旧schemaは持たず、この2モデルの合成保存状態だけを明示する。
    models = raw["inputs"]["generation"]["models"]
    assert [model["model_id"] for model in models] == ["company_growth", OLD]
    models[1]["model_id"] = NEW
    raw["inputs"]["generation"]["models"] = [models[1], models[0]]
    if generation_version == 2:
        raw["inputs"]["generation"]["subject_plan"]["1234"]["model_ids"] = [
            NEW,
            "company_growth",
        ]
    raw["inputs"]["screening_selection"]["result"]["cards"][0]["model_revisions"] = {
        NEW: "original-calculation"
    }
    operational = parse_selection(raw)
    operation = case.operations.get(case.operation.operation_id)
    assert operation.status == "active"
    payload = operation.payload.model_dump(mode="json")
    payload["result"] = "架空の有限判断完了"
    payload["next"] = "本人の判断は別記録"
    for index, artifact in enumerate(payload["artifacts"]):
        if artifact.get("kind") == "research_selection":
            payload["artifacts"][index] = operational.model_dump(mode="json")
    if damage == "duplicate_selection":
        payload["artifacts"].append(operational.model_dump(mode="json"))
    with sqlite3.connect(case.app) as connection:
        # 完了serviceや変換の試験ではなく、現schemaの既知の保存状態をseedする。
        # 通常writerが作った原署名を保ち、completed行のUPDATE/DELETEは行わない。
        connection.execute(
            "UPDATE operation_session SET status=?,completed_at=?,payload=? WHERE operation_id=?",
            (
                "active" if damage == "active" else "completed",
                None if damage == "active" else (PUBLISHED + timedelta(minutes=2)).isoformat(),
                canonical_json(payload),
                case.operation.operation_id,
            ),
        )
        if damage != "no_receipt":
            connection.execute(
                "INSERT INTO selection_identity_migration VALUES (?,?,?,?,?,?)",
                (
                    case.operation.operation_id,
                    "company-financials-id-v1",
                    AT.isoformat(),
                    original.research_scope.selection_sha256,
                    "f" * 64 if damage == "operational_hash" else selection_sha256(operational),
                    published + " " if damage == "canonical" else published,
                ),
            )
    return case, original, operational, published


@pytest.mark.parametrize("generation_version", [1, 2])
@pytest.mark.parametrize("selection_version", [3, 4])
def test_original_signature_human_report_and_original_index_stay_exact(
    tmp_path, monkeypatch, generation_version, selection_version
):
    case, original, operational, published = evidence_case(
        tmp_path,
        monkeypatch,
        generation_version=generation_version,
        selection_version=selection_version,
    )
    before = hashlib.sha256(case.app.read_bytes()).hexdigest()
    assert get_investment_proposal(proposal_id=original.proposal_id, db_path=case.app) == original
    with closing(connect_read_only(case.app)) as connection:
        assert load_latest_investment_proposal(connection).proposal_id == original.proposal_id
        report = proposal_records(connection, original.proposal_id)["human_reports"][0]
        assert report["proposal_ref"] == reference(original).model_dump(mode="json")
        assert report["outcome"] == "not_purchased"
        assert report["reason_quote"] == "様子を見る"
    old = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=original.research_scope.selection_sha256,
        proposal_id=original.proposal_id,
        db_path=case.app,
    )
    new = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=selection_sha256(operational),
        db_path=case.app,
    )
    assert (old["role"], new["role"]) == ("as_published", "operational")
    assert old["canonical_json"] == published
    assert new["canonical_json"] == canonical_json(operational.model_dump(mode="json"))
    assert old["operational_selection_sha256"] == new["selection_sha256"]
    a = json.loads(old["canonical_json"])["inputs"]["generation"]["models"]
    b = json.loads(new["canonical_json"])["inputs"]["generation"]["models"]
    assert [model["model_id"] for model in a] == ["company_growth", OLD]
    assert [model["model_id"] for model in b] == [NEW, "company_growth"]
    assert (
        a[1]["payload"]
        == b[0]["payload"]
        == {
            "amount": "123456789.000",
            "decimals": "-3",
            "unknown": None,
        }
    )
    assert before == hashlib.sha256(case.app.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "damage", ["active", "operational_hash", "canonical", "duplicate_selection"]
)
def test_damaged_receipt_or_operation_is_not_reinterpreted(tmp_path, monkeypatch, damage):
    case, original, _, _ = evidence_case(tmp_path, monkeypatch, damage=damage)
    with pytest.raises(ValueError, match=r"exact inputs|one fixed selection"):
        read_selection_evidence(
            operation_id=case.operation.operation_id,
            sha256=original.research_scope.selection_sha256,
            proposal_id=original.proposal_id,
            db_path=case.app,
        )


def test_historical_evidence_requires_exact_proposal_and_hash(tmp_path, monkeypatch):
    case, original, operational, _ = evidence_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="exact published"):
        read_selection_evidence(
            operation_id=case.operation.operation_id,
            sha256=original.research_scope.selection_sha256,
            db_path=case.app,
        )
    with pytest.raises(ValueError, match="proposal scope differs"):
        read_selection_evidence(
            operation_id=case.operation.operation_id,
            sha256=selection_sha256(operational),
            proposal_id=original.proposal_id,
            db_path=case.app,
        )
    with pytest.raises(ValueError, match="exact published"):
        read_selection_evidence(
            operation_id=case.operation.operation_id, sha256="f" * 64, db_path=case.app
        )
    with pytest.raises(ValueError, match="unavailable"):
        read_selection_evidence(operation_id="missing", sha256="f" * 64, db_path=case.app)


def test_current_inputs_need_no_receipt_but_history_cannot_fall_back(tmp_path, monkeypatch):
    case, original, operational, _ = evidence_case(tmp_path, monkeypatch, damage="no_receipt")
    current = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=selection_sha256(operational),
        db_path=case.app,
    )
    assert current["role"] == "operational"
    with pytest.raises(ValueError, match="exact published"):
        read_selection_evidence(
            operation_id=case.operation.operation_id,
            sha256=original.research_scope.selection_sha256,
            proposal_id=original.proposal_id,
            db_path=case.app,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE selection_identity_migration SET recorded_at='changed'",
        "DELETE FROM selection_identity_migration",
    ],
)
def test_publication_evidence_is_immutable(tmp_path, monkeypatch, sql):
    case, _, _, _ = evidence_case(tmp_path, monkeypatch)
    with (
        sqlite3.connect(case.app) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(sql)


def test_old_workspace_is_archived_as_a_whole_and_never_rebound(tmp_path, monkeypatch):
    case, original, _, _ = evidence_case(tmp_path, monkeypatch)
    source = tmp_path / "old-workspace"
    source.mkdir()
    (source / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "operation_id": case.operation.operation_id,
                "research_as_of": "2026-10-14",
                "purpose": "fundamental_research",
                "selection_sha256": original.research_scope.selection_sha256,
            }
        )
    )
    (source / "domain-inputs.yaml").write_text("original model index: 1\n")
    (source / "assessment-draft.yaml").write_text("source: /inputs/generation/models/1/payload\n")
    archive, output = tmp_path / "archive", tmp_path / "workspace"
    result = prepare_workspace(
        db=case.app,
        operation_id=case.operation.operation_id,
        proposal_id=original.proposal_id,
        source=source,
        archive=archive,
        output=output,
        now=AT,
    )
    for name in ("manifest.yaml", "domain-inputs.yaml", "assessment-draft.yaml"):
        assert (archive / name).read_bytes() == (source / name).read_bytes()
    assert not (output / "assessment-draft.yaml").exists()
    assert result["operational_selection_sha256"] != result["published_selection_sha256"]
    assert OperationService(case.app).get(case.operation.operation_id).status == "completed"
    assert "manifest.yaml" in result["operational_files"]
    with pytest.raises(ResearchWorkspaceConflictError, match="workspace differs"):
        resume_selected_workspace(
            operation_id=case.operation.operation_id,
            db_path=case.app,
            workspace=source,
            now=AT,
            force=True,
        )
