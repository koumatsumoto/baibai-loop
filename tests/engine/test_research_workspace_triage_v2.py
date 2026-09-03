from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_triage import (
    published_review_set,
    research_entry,
    research_triage_payload,
    skip_entry,
)

from baibai_engine.operation.models import OperationPayload
from baibai_engine.operation.service import OperationService
from baibai_engine.research import workspace as workspace_module
from baibai_engine.research.close_source import PreviousClose
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    compute_status,
    prepare_workspace,
)
from baibai_engine.research.workspace_cli import main as research_main
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.research_triage import (
    ResearchTriage,
    ResearchTriageService,
)


def _publish_triage(
    db_path: Path, *, research: bool = True, second_research: bool = False
) -> ResearchTriage:
    ledger = load_portfolio_ledger(
        Path(__file__).resolve().parents[1] / "fixtures/portfolio-ledger/representative.yaml"
    )
    seed_ledger(
        db_path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )
    if not research:
        entries = [skip_entry("2331"), skip_entry("0001")]
    elif second_research:
        entries = [research_entry("2331", rank=1), research_entry("0001", rank=2)]
    else:
        entries = [research_entry("2331", rank=1), skip_entry("0001")]
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id="research-triage-workspace-v2",
            review_set_id="review-set-workspace-v2",
            run_revision_id="runrev-workspace-v2",
            entries=entries,
        )
    )
    review_set = PublishedReviewSet.model_validate(
        published_review_set(
            review_set_id=triage.review_set_id,
            run_revision_id=triage.run_revision_id,
            tickers=("2331", "0001"),
        )
    )
    return ResearchTriageService(db_path).publish(triage, review_set=review_set)


def test_prepare_and_status_need_only_published_triage_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_module, "ER_LEVEL_CALIBRATION_CONTEXT_PATH", tmp_path / "missing-context.yaml"
    )
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    workspace = tmp_path / "workspace"

    assert (
        research_main(
            [
                "prepare",
                "--research-triage-id",
                triage.research_triage_id,
                "--db",
                str(db_path),
                "--workspace",
                str(workspace),
                "--ticker",
                "2331",
            ]
        )
        == 0
    )
    prepared_output = yaml.safe_load(capsys.readouterr().out)

    assert prepared_output["actionable"] is True
    assert prepared_output["researchable_tickers"] == ["2331"]
    assert not (workspace / "review-set.yaml").exists()
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text())
    assert "tool_version" not in manifest
    comparison = yaml.safe_load((workspace / "research-comparison.yaml").read_text())
    assert comparison["er_realized_distribution_context"] == {
        "status": "unavailable",
        "reason": "missing_artifact",
    }
    assert compute_status(workspace, db_path=db_path)["research_triage"] == {
        "purpose": "fundamental_research",
        "research_triage_id": triage.research_triage_id,
        "researchable_tickers": ["2331"],
    }
    operation = OperationService(db_path).active()
    assert operation is not None
    assert operation.payload.canonical_refs == (triage.research_triage_id,)
    assert operation.payload.artifacts[0]["research_set"] == ["2331"]


def test_workspace_rejects_skip_admission_and_payload_hash_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
        research_set=("2331",),
    )
    workspace_path = workspace / "research-workspace.yaml"
    payload = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    payload["research_set"] = ["0001"]
    workspace_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResearchWorkspaceConflictError, match="human-confirmed set"):
        compute_status(workspace, db_path=db_path)

    manifest_path = workspace / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["research_triage"]["payload_sha256"] = "0" * 64
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ResearchWorkspaceConflictError, match="does not match the canonical"):
        compute_status(workspace, db_path=db_path)


def test_zero_research_is_a_normal_prepared_workspace(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path, research=False)

    workspace = tmp_path / "workspace"
    prepared = prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
    )

    assert not prepared.actionable
    assert prepared.researchable_count == 0
    assert prepared.researchable_tickers == ()
    assert compute_status(workspace, db_path=db_path)["workspace_status"] == "no_research"
    assert OperationService(db_path).active() is None


def test_human_can_confirm_empty_research_set_without_starting_operation(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)

    prepared = prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=tmp_path / "workspace",
    )

    assert not prepared.actionable
    assert prepared.researchable_tickers == ("2331",)
    assert OperationService(db_path).active() is None


def test_prepare_rejects_skip_ticker_before_starting_operation(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)

    with pytest.raises(ResearchWorkspaceDataError, match="did not mark research"):
        prepare_workspace(
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=tmp_path / "workspace",
            research_set=("0001",),
        )

    assert OperationService(db_path).active() is None


def test_active_operation_blocks_only_new_research_start(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    service = OperationService(db_path)
    existing = service.start(
        session_kind="position-review",
        as_of=date(2026, 8, 31),
        ticker="2331",
        started_at=datetime.fromisoformat("2026-08-31T18:00:00+09:00"),
        payload=OperationPayload(checkpoint="position review", next="continue"),
    )

    with pytest.raises(ResearchWorkspaceConflictError, match="active operation already exists"):
        prepare_workspace(
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=tmp_path / "workspace",
            research_set=("2331",),
        )

    assert service.active() == existing


def test_force_cannot_overwrite_workspace_with_a_different_active_research_set(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path, second_research=True)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
        research_set=("2331",),
    )
    manifest_before = (workspace / "manifest.yaml").read_bytes()

    with pytest.raises(ResearchWorkspaceConflictError, match="active operation already exists"):
        prepare_workspace(
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=workspace,
            research_set=("0001",),
            force=True,
        )

    assert (workspace / "manifest.yaml").read_bytes() == manifest_before


def test_thesis_scaffold_uses_v3_and_only_general_research_checks() -> None:
    price = PreviousClose(
        close_yen=1000,
        price_as_of=date(2026, 7, 2),
        adjustment_factor=1,
        corporate_action_unresolved=False,
    )

    draft = workspace_module._thesis_draft_skeleton(
        ticker="2331",
        asof=date(2026, 7, 3),
        price=price,
        sqlite_path=Path("unused.sqlite"),
        screening_estimate=None,
        screening_retrieved_at=datetime.fromisoformat("2026-07-03T08:00:00+09:00"),
    )
    checklist = workspace_module._checklist_skeleton(price=price)

    assert draft["schema_version"] == 3
    assert [item["check_id"] for item in checklist["checks"]] == [
        "source.latest_results",
        "source.financial_position",
        "source.cash_flow",
        "source.share_count_and_dilution",
        "source.customer_concentration",
        "source.structural_decline",
        "source.management_accounting_warning",
        "source.corporate_action",
        "scenario.bear_3y_5y",
        "scenario.base_3y_5y",
        "scenario.bull_3y_5y",
        "valuation.fair_value_and_required_cagr",
        "judgment.strongest_countercase",
    ]
