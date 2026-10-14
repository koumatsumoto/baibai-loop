"""固定選定、企業評価と独立Review、immutable公開・再開のworkspace結合。"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests.helpers.assessment_v1 import (
    assessment_id,
    pair_payload,
    update_review_hash,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_selection import draft_for, selection

from baibai_engine.research.assessment import AssessmentError, load_assessment
from baibai_engine.research.operation.progress import progress_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    compute_status,
    prepare_selected_workspace,
    promote,
    scaffold_assessment,
    scaffold_review,
)
from baibai_engine.research.workspace import scaffold as scaffold_module
from baibai_engine.research.workspace_cli import main

NOW = datetime.fromisoformat("2026-10-14T17:00:00+09:00")


@pytest.fixture
def case(tmp_path):
    db, workspace = tmp_path / "app", tmp_path / "workspace"
    value = selection(now=NOW, tickers=("1234",))
    result = prepare_selected_workspace(
        draft=draft_for(value),
        db_path=db,
        workspace=workspace,
        sqlite_path=tmp_path / "absent",
        runs_path=tmp_path / "absent-runs",
        indicators_path=tmp_path / "absent-indicators",
        now=NOW,
    )
    return db, workspace, result.operation_id


def scaffold(case, *, now=NOW, **kwargs):
    db, workspace, _ = case
    return scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=workspace / "absent",
        as_of=now.date(),
        retrieved_at=now,
        db_path=db,
        **kwargs,
    )


def author(case, *, ticker="1234", workspace=None):
    _db, root, _ = case
    root = workspace or root
    document, review = pair_payload(ticker=ticker)
    directory = root / ticker
    directory.mkdir(exist_ok=True)
    (directory / "assessment-draft.yaml").write_text(yaml.safe_dump(document))
    (directory / "assessment-review.yaml").write_text(yaml.safe_dump(review))
    identity = directory / "assessment-publication.yaml"
    if not identity.exists():
        identity.write_text(
            yaml.safe_dump({"assessment_id": assessment_id(), "supersedes_id": None})
        )
    return document, review


def test_initial_scaffold_has_new_contract_and_stable_writer_identity(case):
    first = scaffold(case)
    raw = yaml.safe_load(Path(first["assessment_draft"]).read_text())
    assert raw["kind"] == "stock_assessment"
    assert raw["schema_version"] == 1
    assert raw["findings"]["business_understanding"] is None
    assert raw["valuation"]["horizon_months"] is None
    assert raw["valuation"]["required_annual_return_pct"] is None
    assert "judgment" not in raw
    assert first["assessment_id"].startswith("stock-assessment-")
    with pytest.raises(AssessmentError):
        load_assessment(Path(first["assessment_draft"]))
    with pytest.raises(ResearchWorkspaceConflictError, match="exists"):
        scaffold(case)
    assert scaffold(case, force=True)["assessment_id"] == first["assessment_id"]


def test_scaffold_out_exports_same_payload_and_preserves_canonical_writer_source(case):
    db, workspace, _ = case
    exported = workspace.parent / "custom-draft.yaml"
    result = scaffold(case, out=exported)
    canonical = workspace / "1234/assessment-draft.yaml"
    assert exported.read_bytes() == canonical.read_bytes()
    assert result["exported_draft"] == str(exported)
    exported.write_text("independently edited export")
    assert yaml.safe_load(canonical.read_text())["kind"] == "stock_assessment"
    assert (
        yaml.safe_load((canonical.parent / "assessment-publication.yaml").read_text())[
            "assessment_id"
        ]
        == result["assessment_id"]
    )
    author(case)
    promoted = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert promoted.assessment_id == result["assessment_id"]


def test_scaffold_export_collision_has_no_workspace_write(case):
    _db, workspace, _ = case
    exported = workspace.parent / "custom-draft.yaml"
    exported.write_text("existing authored file")
    with pytest.raises(ResearchWorkspaceConflictError, match="export exists"):
        scaffold(case, out=exported)
    assert not (workspace / "1234").exists()
    assert exported.read_text() == "existing authored file"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_cached_price_does_not_create_draft(case, monkeypatch, invalid):
    monkeypatch.setattr(
        scaffold_module,
        "read_unadjusted_close",
        lambda **_kw: SimpleNamespace(
            close_yen=invalid,
            price_as_of=NOW.date(),
        ),
    )
    with pytest.raises(ResearchWorkspaceDataError, match=r"finite|number"):
        scaffold(case)
    assert not (case[1] / "1234").exists()


def test_review_scaffold_has_exact_hash_new_uuid_and_independent_empty_slots(case):
    db, workspace, _ = case
    author(case)
    (workspace / "1234/assessment-review.yaml").unlink()
    result = scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    raw = yaml.safe_load(Path(result["review_draft"]).read_text())
    assert Path(result["review_draft"]).name == "assessment-review.yaml"
    assert raw["review_id"].startswith("assessment-review-")
    assert raw["reviewed_assessment_sha256"] == result["reviewed_assessment_sha256"]
    assert raw["review_scope"] is None
    assert raw["checked_source_ids"] == []
    assert [row["terminal_value_per_share_yen"] for row in raw["recalculated_projections"]] == [
        None,
        None,
    ]


def test_promote_records_exact_assessment_for_new_target_and_retry_preserves_id(case):
    db, workspace, operation_id = case
    scaffold(case)
    author(case)
    inputs = (workspace / "domain-inputs.yaml").read_bytes()
    first = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert (
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(days=1))
        == first
    )
    progress = progress_binding(OperationService(db).get(operation_id).payload)
    assert progress.outcomes[0].assessment_ref.assessment_id == first.assessment_id
    assert progress.outcomes[0].purpose == "new"
    status = compute_status(workspace, db_path=db, now=NOW + timedelta(days=2))
    assert status["cases"][0]["assessment_id"] == first.assessment_id
    assert status["cases"][0]["status"] == "published"
    assert (workspace / "domain-inputs.yaml").read_bytes() == inputs
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0] == 0


def test_status_requires_exact_pair_and_no_stale_review_promotion(case):
    db, workspace, _ = case
    scaffold(case)
    document, review = author(case)
    promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    document["findings"]["business_understanding"] += "新しい調査文"
    draft = workspace / "1234/assessment-draft.yaml"
    draft.write_text(yaml.safe_dump(document))
    status = compute_status(workspace, db_path=db, now=NOW)
    assert status["cases"][0]["status"] == "ready_for_review"
    assert status["cases"][0]["assessment_id"] is None
    with pytest.raises(ResearchWorkspaceDataError, match="stale"):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    update_review_hash(document, review)
    (workspace / "1234/assessment-review.yaml").write_text(yaml.safe_dump(review))
    assert (
        compute_status(workspace, db_path=db, now=NOW)["cases"][0]["status"]
        == "ready_for_promotion"
    )
    with pytest.raises(ResearchWorkspaceConflictError, match="immutable"):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)


def test_refresh_scaffold_preserves_sources_and_requires_new_horizon_and_review(case, monkeypatch):
    db, workspace, _ = case
    scaffold(case)
    old, _ = author(case)
    published = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    monkeypatch.setattr(
        scaffold_module, "read_unadjusted_close", lambda **_kw: pytest.fail("refresh read quote")
    )
    refreshed = scaffold(
        case, now=NOW + timedelta(days=1), force=True, from_assessment_id=published.assessment_id
    )
    raw = yaml.safe_load(Path(refreshed["assessment_draft"]).read_text())
    assert refreshed["assessment_id"] != published.assessment_id
    assert raw["input_snapshot"]["sources"] == old["input_snapshot"]["sources"]
    assert raw["input_snapshot"]["facts"] == old["input_snapshot"]["facts"]
    assert raw["valuation"]["horizon_months"] is None
    assert raw["valuation"]["base"]["cash_distribution_per_share_yen"] is None
    assert raw["refresh"]["checked_at"] is None
    assert raw["refresh"]["horizon_reason"] is None
    assert raw["refresh"]["prior_assessment_ref"]["assessment_id"] == published.assessment_id
    with pytest.raises(ResearchWorkspaceDataError):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(days=1))


def test_holding_workspace_promote_records_the_same_assessment_contract(tmp_path):
    db = tmp_path / "app"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger.model_copy(update={"market_prices": ()}))
    value = selection(now=NOW, tickers=("2331",), scope="review_positions")
    prepared = prepare_selected_workspace(
        draft=draft_for(value),
        db_path=db,
        workspace=tmp_path / "workspace",
        sqlite_path=tmp_path / "absent",
        runs_path=tmp_path / "absent-runs",
        indicators_path=tmp_path / "absent-indicators",
        now=NOW,
    )
    child = prepared.workspace / "holdings/2331"
    author((db, child, prepared.operation_id), ticker="2331")
    before = ledger.model_dump(mode="json")
    published = promote(workspace=child, ticker="2331", db_path=db, now=NOW)
    progress = progress_binding(OperationService(db).get(prepared.operation_id).payload)
    assert progress.outcomes[0].purpose == "holding"
    assert progress.outcomes[0].assessment_ref.assessment_id == published.assessment_id
    assert (
        compute_status(prepared.workspace, db_path=db, now=NOW)["workspace_status"] == "published"
    )
    from baibai_engine.position.store import LedgerStoreService

    assert LedgerStoreService(db).load().model_dump(mode="json") == {**before, "market_prices": []}


def test_new_public_cli_uses_scaffolded_identity_and_rejects_retired_commands(case, capsys):
    db, workspace, _ = case
    assert (
        main(
            [
                "assessment-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
                "--asof",
                NOW.date().isoformat(),
                "--out",
                str(workspace / "1234/assessment-draft.yaml"),
            ],
            now=NOW,
        )
        == 0
    )
    identity = yaml.safe_load(capsys.readouterr().out)["assessment_id"]
    author(case)
    assert (
        main(
            [
                "assessment-promote",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
            ],
            now=NOW,
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["assessment_id"] == identity
    for command in (
        "thesis-scaffold",
        "evaluate",
        "promote",
        "plan-limit",
        "capital-allocation-publish",
    ):
        with pytest.raises(SystemExit) as exc:
            main([command], now=NOW)
        assert exc.value.code == 2
