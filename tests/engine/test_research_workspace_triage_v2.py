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
from baibai_engine.position.ledger import ContributionEvent
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research import workspace as workspace_module
from baibai_engine.research.market_close_source import UnadjustedCloseObservation
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

CASE_NOW = datetime.fromisoformat("2026-07-19T12:00:00+09:00")


def test_research_resumes_reordered_set_and_keeps_prepare_context(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db, second_research=True)
    workspace = tmp_path / "workspace"
    context_file = tmp_path / "calibration.yaml"
    context_file.write_text("original calibration")
    context = {"generated_at": "2026-07-19T09:00:00+09:00", "summary": "prepare context"}
    monkeypatch.setattr(
        workspace_module,
        "_load_er_distribution_context",
        lambda **kwargs: (
            context,
            {"path": str(context_file), "sha256": workspace_module._sha256_file(context_file)},
        ),
    )
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
    )
    operation = OperationService(db).active()
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("0001", "2331"),
        force=True,
    )
    assert OperationService(db).active() == operation
    _authored_case(workspace, "2331", "reject")
    before = (workspace / "research-workspace.yaml").read_bytes()
    ledger = LedgerStoreService(db)
    original, head = ledger.load_with_head()
    event = ContributionEvent(
        type="contribution",
        event_id="unrelated-deposit",
        occurred_at=original.as_of,
        amount_yen=1000,
    )
    ledger.apply_document(
        expected_head=head,
        expected_document=original,
        replacement=original.model_copy(update={"events": (*original.events, event)}),
    )
    assert ledger.append_head() > head
    for change in (lambda: context_file.write_text("new calibration"), context_file.unlink):
        change()
        status = compute_status(workspace, db_path=db, now=CASE_NOW)
        assert status["cases"][1]["status"] == "ready_for_promotion"
        assert (workspace / "research-workspace.yaml").read_bytes() == before
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id=None,
        supersedes_id=None,
        now=CASE_NOW,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "proposal",
        "arithmetic",
        "review-missing",
        "stale-core",
        "arithmetic-missing",
        "arithmetic-stale",
    ],
)
def test_status_and_promote_share_case_eligibility(tmp_path: Path, mutation: str, mocker) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    _authored_case(workspace, "2331", "reject")
    thesis_path = workspace / "2331/thesis-draft.yaml"
    review_path = workspace / "2331/2026-07-19-2331-decision-review.yaml"
    review = yaml.safe_load(review_path.read_text())
    if mutation == "proposal":
        review.update(proposal_changed=True, change_rationale="revise the valuation")
    elif mutation == "stale-core":
        review["reviewed_thesis_sha256"] = "0" * 64
    elif mutation.startswith("arithmetic"):
        from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash

        raw = yaml.safe_load(thesis_path.read_text())
        raw["estimates"]["scenarios"][0]["claimed_total_return_cagr_pct"] = 999
        thesis_path.write_text(yaml.safe_dump(raw))
        review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(raw))
        if mutation == "arithmetic-stale":
            review["reviewed_thesis_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review))
    if mutation in {"review-missing", "arithmetic-missing"}:
        review_path.unlink()
    read = mocker.spy(workspace_module, "_load_mapping")
    status = compute_status(workspace, db_path=db, now=CASE_NOW)
    case = status["cases"][0]
    assert case["status"] != "ready_for_promotion"
    errors = case["thesis_validation_errors"] + case["review_validation_errors"]
    assert errors
    if mutation.startswith("arithmetic"):
        assert case["status"] == "incomplete"
        assert any("CAGR mismatch" in error for error in case["thesis_validation_errors"])
    assert sum(call.args[0] == thesis_path for call in read.call_args_list) == 1
    with pytest.raises(ResearchWorkspaceDataError) as error:
        workspace_module.promote(
            workspace=workspace,
            ticker="2331",
            db_path=db,
            thesis_id=None,
            supersedes_id=None,
            now=CASE_NOW,
        )
    assert errors[0] in str(error.value)


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
    assert prepared_output["admissible_research_tickers"] == ["2331"]
    assert not (workspace / "review-set.yaml").exists()
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text())
    assert "tool_version" not in manifest
    assert not (workspace / "research-comparison.yaml").exists()
    comparison = yaml.safe_load((workspace / "research-workspace.yaml").read_text())
    assert comparison["er_realized_distribution_context"] == {
        "status": "unavailable",
        "reason": "missing_artifact",
    }
    assert compute_status(
        workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )["research_triage"] == {
        "purpose": "fundamental_research",
        "research_triage_id": triage.research_triage_id,
        "admissible_research_tickers": ["2331"],
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
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )

    manifest_path = workspace / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["research_triage"]["payload_sha256"] = "0" * 64
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ResearchWorkspaceConflictError, match="does not match the canonical"):
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )


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
    assert prepared.admissible_count == 0
    assert prepared.admissible_research_tickers == ()
    assert (
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "no_research"
    )
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
    assert prepared.admissible_research_tickers == ("2331",)
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
    price = UnadjustedCloseObservation(
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


def _authored_case(workspace: Path, ticker: str, recommendation: str) -> None:
    from baibai_engine.research.thesis import (
        ThesisDocument,
        ThesisReview,
        thesis_core_hash,
        thesis_review_hash,
    )

    fixture = Path("tests/fixtures/thesis")
    raw = yaml.safe_load(
        (fixture / "2331-decision.yaml")
        .read_text()
        .replace("2331", ticker)
        .replace("2026-07-03", "2026-07-19")
    )
    review = yaml.safe_load(
        (fixture / "2331-decision-review.yaml")
        .read_text()
        .replace("2331", ticker)
        .replace("2026-07-03", "2026-07-19")
    )
    review_name = f"2026-07-19-{ticker}-decision-review.yaml"
    raw["independent_review_ref"] = review_name
    raw["judgment"]["recommendation"] = recommendation
    raw["human_evidence_override"] = None
    core = thesis_core_hash(ThesisDocument.model_validate(raw))
    review["reviewed_thesis_sha256"] = core
    if recommendation == "buy":
        override = yaml.safe_load((fixture / "2331-decision.yaml").read_text())[
            "human_evidence_override"
        ]
        override.update(
            approved_at="2026-07-19T10:30:00+09:00",
            thesis_sha256=core,
            review_id=review["review_id"],
            review_sha256=thesis_review_hash(ThesisReview.model_validate(review)),
        )
        raw["human_evidence_override"] = override
    directory = workspace / ticker
    directory.mkdir(exist_ok=True)
    (directory / "thesis-draft.yaml").write_text(yaml.safe_dump(raw))
    (directory / review_name).write_text(yaml.safe_dump(review))
    (directory / "research-checklist.yaml").write_text(
        yaml.safe_dump(
            {
                "checks": [
                    {"check_id": item, "status": "complete"}
                    for item in workspace_module.CHECKLIST_IDS
                ],
            }
        )
    )


@pytest.mark.parametrize("recommendations", [("buy", "reject"), ("defer", "reject")])
def test_all_cases_promote_without_a_winner_and_status_tracks_exact_publication(
    tmp_path: Path, recommendations: tuple[str, str]
) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db, second_research=True)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["ticker"] for case in status["cases"]] == ["2331", "0001"]
    assert all(case["status"] == "incomplete" for case in status["cases"])
    assert "selected_ticker" not in status
    assert not (workspace / "research-comparison.yaml").exists()
    for ticker, recommendation in zip(("2331", "0001"), recommendations, strict=True):
        _authored_case(workspace, ticker, recommendation)
    assert all(
        case["status"] == "ready_for_promotion"
        for case in compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"]
    )
    now = datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    workspace_module.promote(
        workspace=workspace,
        ticker="0001",
        db_path=db,
        thesis_id="thesis-second",
        supersedes_id=None,
        now=now,
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["status"] for case in status["cases"]] == ["ready_for_promotion", "published"]
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="thesis-first",
        supersedes_id=None,
        now=now,
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert status["workspace_status"] == "published"
    assert status["next_action"] is None
    assert [case["thesis_id"] for case in status["cases"]] == ["thesis-first", "thesis-second"]
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2027-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    # Re-prepare keeps authored drafts; status derives publication without a local journal.
    authored = (workspace / "2331/thesis-draft.yaml").read_bytes()
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
        force=True,
    )
    assert (workspace / "2331/thesis-draft.yaml").read_bytes() == authored
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    review_path = workspace / "0001/2026-07-19-0001-decision-review.yaml"
    review = yaml.safe_load(review_path.read_text())
    review["reviewed_thesis_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review))
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"][1]["status"]
        == "ready_for_review"
    )
    with pytest.raises(ResearchWorkspaceDataError, match="stale"):
        workspace_module.promote(
            workspace=workspace,
            ticker="0001",
            db_path=db,
            thesis_id=None,
            supersedes_id=None,
            now=now,
        )


def test_holding_workspace_has_one_ledger_subject_without_comparison(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _publish_triage(db)
    workspace = tmp_path / "holding"
    workspace_module.prepare_holding_workspace(
        db_path=db, workspace=workspace, ticker="2331", asof=date(2026, 7, 11)
    )
    assert not (workspace / "research-comparison.yaml").exists()
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["ticker"] for case in status["cases"]] == ["2331"]
    assert status["research_triage"]["research_triage_id"] is None
    path = workspace / "research-workspace.yaml"
    document = yaml.safe_load(path.read_text())
    document["research_set"] = ["0001"]
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ResearchWorkspaceDataError, match="holding subject fixed"):
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )


def test_same_core_with_edited_override_is_not_the_published_draft(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    _authored_case(workspace, "2331", "buy")
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="thesis-original",
        supersedes_id=None,
        now=datetime.fromisoformat("2026-07-19T12:00:00+09:00"),
    )
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    path = workspace / "2331/thesis-draft.yaml"
    draft = yaml.safe_load(path.read_text())
    draft["human_evidence_override"]["reason"] = "different human acknowledgement"
    path.write_text(yaml.safe_dump(draft))
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"][0]["status"]
        == "ready_for_promotion"
    )
