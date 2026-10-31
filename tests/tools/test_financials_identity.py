"""旧schemaの架空署名済み判断を、原検証/本人報告/出典indexごと検証する。"""

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from tests.helpers import generated_selection as generation_helpers
from tests.helpers import proposal as proposal_helpers
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tools.financials_identity import (
    NEW,
    OLD,
    _policy,
    file_sha256,
    prepare_copy,
)
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
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import GeneratedResearchSelection
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
from baibai_engine.storage.sqlite.application.read import connect_read_only

AT = PUBLISHED + timedelta(days=1)
WRONG = "investment-proposal-00000000-0000-4000-8000-000000000004"


def reference(proposal):
    return ProposalReference(
        proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
    )


def legacy_case(
    tmp_path,
    monkeypatch,
    *,
    active=False,
    without_proposal=False,
    generation_version=1,
    selection_version=4,
):
    """旧model IDの入力を先に作り、その内容に対し架空作者が署名する。"""
    if generation_version == 2:
        generation_type = generation_helpers.ModelGeneration

        def generation(**kwargs):
            return generation_type(
                **kwargs,
                schema_version=2,
                subject_plan={
                    "1234": GenerationSubject(scope="stock", model_ids=(OLD, "company_growth"))
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
    case = backend_case(tmp_path)
    original = None
    if without_proposal:
        case.operations.record_unfinished(
            case.operation.operation_id,
            ticker="1234",
            reason="架空の調査中断",
            references=("synthetic-unfinished",),
            now=PUBLISHED,
        )
    else:
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
    if not active:
        case.operations.complete(
            case.operation.operation_id,
            result="架空の有限判断完了",
            next="本人の判断は別記録",
            completed_at=PUBLISHED + timedelta(minutes=2),
        )
    # 空の追加tableを除き、旧schemaそのもののfixtureとする。原Reviewを再署名しない。
    with sqlite3.connect(case.app) as connection:
        connection.execute("DROP TABLE selection_identity_migration")
        connection.execute("PRAGMA user_version=28")
    with sqlite3.connect(case.runs) as connection:
        connection.execute("PRAGMA user_version=7")
    return case, original


def prepare(case, target):
    return prepare_copy(source_app=case.app, source_runs=case.runs, output=target, recorded_at=AT)


def rows(path, table):
    with sqlite3.connect(path) as connection:
        return sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)


@pytest.mark.parametrize("generation_version", [1, 2])
@pytest.mark.parametrize("selection_version", [3, 4])
def test_original_signature_human_report_and_original_index_stay_exact(
    tmp_path, monkeypatch, generation_version, selection_version
):
    case, original = legacy_case(
        tmp_path,
        monkeypatch,
        generation_version=generation_version,
        selection_version=selection_version,
    )
    before = (file_sha256(case.app), file_sha256(case.runs))
    target = tmp_path / "normalized"
    result = prepare(case, target)
    app = target / "application.sqlite"
    assert prepare(case, target) == result
    assert before == (file_sha256(case.app), file_sha256(case.runs))
    assert result["schemas"] == {"application": 29, "runs": 8}
    assert result["verification"]["human_reports_verified"] == 1
    assert result["verification"]["retained_policy_matches"] == 1
    assert get_investment_proposal(proposal_id=original.proposal_id, db_path=app) == original
    for table in (
        "investment_proposal",
        "stock_assessment",
        "stock_assessment_review",
        "proposal_human_report",
        "proposal_correction",
        "position_decision_reference",
        "ledger_event",
        "ledger_meta",
    ):
        assert rows(case.app, table) == rows(app, table)
    with closing(connect_read_only(app)) as connection:
        assert load_latest_investment_proposal(connection).proposal_id == original.proposal_id
        assert proposal_records(connection, original.proposal_id)["human_reports"][0][
            "proposal_ref"
        ] == reference(original).model_dump(mode="json")
    old = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=original.research_scope.selection_sha256,
        proposal_id=original.proposal_id,
        db_path=app,
    )
    new = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=old["operational_selection_sha256"],
        db_path=app,
    )
    assert (old["role"], new["role"]) == ("as_published", "operational")
    a = json.loads(old["canonical_json"])["inputs"]["generation"]["models"]
    b = json.loads(new["canonical_json"])["inputs"]["generation"]["models"]
    old_index = next(i for i, model in enumerate(a) if model["model_id"] == OLD)
    new_index = next(i for i, model in enumerate(b) if model["model_id"] == NEW)
    assert old_index != new_index
    assert (
        a[old_index]["payload"]
        == b[new_index]["payload"]
        == {
            "amount": "123456789.000",
            "decimals": "-3",
            "unknown": None,
        }
    )
    with pytest.raises(ValueError, match="exact published"):
        read_selection_evidence(
            operation_id=case.operation.operation_id,
            sha256=original.research_scope.selection_sha256,
            db_path=app,
        )
    with (
        sqlite3.connect(app) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute("DELETE FROM selection_identity_migration")


def test_old_workspace_is_archived_as_a_whole_and_never_rebound(tmp_path, monkeypatch):
    case, original = legacy_case(tmp_path, monkeypatch)
    target = tmp_path / "normalized"
    prepare(case, target)
    app = target / "application.sqlite"
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
    (source / "domain-inputs.yaml").write_text("original model index: 0\n")
    (source / "assessment-draft.yaml").write_text("source: /inputs/generation/models/0/payload\n")
    archive, output = tmp_path / "archive", tmp_path / "workspace"
    result = prepare_workspace(
        db=app,
        operation_id=case.operation.operation_id,
        proposal_id=original.proposal_id,
        source=source,
        archive=archive,
        output=output,
        now=AT,
    )
    assert (archive / "assessment-draft.yaml").read_bytes() == (
        source / "assessment-draft.yaml"
    ).read_bytes()
    assert not (output / "assessment-draft.yaml").exists()
    assert result["operational_selection_sha256"] != result["published_selection_sha256"]
    assert OperationService(app).get(case.operation.operation_id).status == "completed"
    assert "manifest.yaml" in result["operational_files"]
    with pytest.raises(ResearchWorkspaceConflictError, match="workspace differs"):
        resume_selected_workspace(
            operation_id=case.operation.operation_id,
            db_path=app,
            workspace=source,
            now=AT,
            force=True,
        )


@pytest.mark.parametrize(
    "mode", ["active", "missing_proposal", "wal", "escaped_id", "collision", "policy_drift"]
)
def test_unsupported_sources_stop_without_changing_originals(tmp_path, monkeypatch, mode):
    case, _ = legacy_case(
        tmp_path, monkeypatch, active=mode == "active", without_proposal=mode == "missing_proposal"
    )
    if mode == "wal":
        case.app.with_name(case.app.name + "-wal").write_bytes(b"pending")
    if mode in {"escaped_id", "collision", "policy_drift"}:
        with sqlite3.connect(case.runs) as connection:
            if mode == "escaped_id":
                payload = gzip.compress(b'{"unexpected_model_id":"company\\u005fvaluation"}')
                connection.execute(
                    "UPDATE generated_model SET payload_gzip=? WHERE model_id=?", (payload, OLD)
                )
            elif mode == "collision":
                connection.execute(
                    "INSERT INTO generated_model SELECT generation_id,?,subject,calculation_revision,payload_gzip "
                    "FROM generated_model WHERE model_id=?",
                    (NEW, OLD),
                )
            else:
                row = connection.execute(
                    "SELECT selection_id,payload_gzip FROM screening_selection"
                ).fetchone()
                payload = json.loads(gzip.decompress(row[1]))
                payload["result"]["eligible_count"] = 2
                connection.execute(
                    "UPDATE screening_selection SET payload_gzip=? WHERE selection_id=?",
                    (gzip.compress(json.dumps(payload).encode()), row[0]),
                )
    before = (file_sha256(case.app), file_sha256(case.runs))
    expected_error = {
        "active": "active Operation",
        "missing_proposal": "without a published Proposal",
        "wal": "self-contained",
        "escaped_id": "unknown field",
        "collision": "collide",
        "policy_drift": "policy copy differs",
    }
    with pytest.raises(ValueError, match=expected_error[mode]):
        prepare(case, tmp_path / "rejected")
    assert before == (file_sha256(case.app), file_sha256(case.runs))


def test_candidate_missing_identity_is_typed_and_key_collision_is_rejected():
    raw = {
        "generation_id": "fixture",
        "universe_tickers": ["1234"],
        "result": {
            "policy": {"method": "best-economic-path-v4", "candidate_limit": 36},
            "eligible_count": 1,
            "cards": [
                {
                    "ticker": "1234",
                    "name": None,
                    "sector": None,
                    "rank": 1,
                    "priority": 0.5,
                    "strongest_path": "earning_power",
                    "paths": [],
                    "context": {},
                    "model_revisions": {},
                    "not_generated": [OLD],
                }
            ],
        },
    }
    assert _policy(raw)["result"]["cards"][0]["not_generated"] == [NEW]
    raw["result"]["cards"][0]["model_revisions"] = {OLD: "v1", NEW: "v1"}
    with pytest.raises(ValueError, match="collide"):
        _policy(raw)


def test_future_clock_is_not_a_migration_record(tmp_path):
    with pytest.raises(ValueError, match="future"):
        prepare_copy(
            source_app=tmp_path / "absent",
            source_runs=tmp_path / "absent-runs",
            output=tmp_path / "output",
            recorded_at=datetime.now(UTC) + timedelta(days=1),
        )
