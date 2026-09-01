from __future__ import annotations

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


def _publish_triage(db_path: Path, *, research: bool = True) -> ResearchTriage:
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
    entries = (
        [research_entry("2331", rank=1), skip_entry("0001")]
        if research
        else [skip_entry("2331"), skip_entry("0001")]
    )
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            ]
        )
        == 0
    )
    prepared_output = yaml.safe_load(capsys.readouterr().out)

    assert prepared_output["actionable"] is True
    assert prepared_output["researchable_tickers"] == ["2331"]
    assert not (workspace / "review-set.yaml").exists()
    assert compute_status(workspace, db_path=db_path)["research_triage"] == {
        "purpose": "fundamental_research",
        "research_triage_id": triage.research_triage_id,
        "researchable_tickers": ["2331"],
    }


def test_workspace_rejects_skip_admission_and_payload_hash_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
    )
    workspace_path = workspace / "research-workspace.yaml"
    payload = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    payload["research_set"] = ["0001"]
    workspace_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResearchWorkspaceDataError, match="did not mark research"):
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
    assert prepared.research_capacity == 0
    assert prepared.researchable_tickers == ()
    assert compute_status(workspace, db_path=db_path)["workspace_status"] == "no_research"
