"""固定選定、企業評価と独立Review、immutable公開・再開のworkspace結合。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
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
from baibai_engine.research.assessment_store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
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


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("alias", [False, True])
def test_scaffold_export_cannot_replace_its_new_publication_identity(case, force, alias):
    _db, workspace, _ = case
    exported = workspace / "1234/assessment-publication.yaml"
    if alias:
        linked = workspace.parent / "identity-export.yaml"
        linked.symlink_to(exported)
        exported = linked
    with pytest.raises(ResearchWorkspaceConflictError, match="publication identity"):
        scaffold(case, out=exported, force=force)
    assert not (workspace / "1234").exists()


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


@pytest.mark.parametrize(
    ("as_of", "retrieved_at", "expected_date", "expected_price"),
    [
        ("2026-10-14", "2026-10-15T17:00:00+09:00", "2026-10-14", 100.0),
        ("2026-10-15", "2026-10-15T09:00:00+09:00", "2026-10-14", 100.0),
        ("2026-10-15", "2026-10-15T17:00:00+09:00", "2026-10-15", 200.0),
    ],
)
def test_scaffold_quote_respects_assessment_date_and_available_close(
    case, as_of, retrieved_at, expected_date, expected_price
):
    from tests.helpers.screening_sqlite import seed_daily_bars

    db, workspace, _ = case
    market = workspace / "market.sqlite"
    seed_daily_bars(
        market,
        [("1234", "2026-10-14", 100.0, 1.0), ("1234", "2026-10-15", 200.0, 1.0)],
    )
    result = scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=market,
        as_of=datetime.fromisoformat(as_of).date(),
        retrieved_at=datetime.fromisoformat(retrieved_at),
        db_path=db,
    )
    raw = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert result["price_as_of"] == expected_date
    assert result["close_yen"] == expected_price
    assert raw["input_snapshot"]["facts"][0]["as_of"] == expected_date
    assert raw["input_snapshot"]["sources"][0]["retrieved_at"] == retrieved_at


def test_review_scaffold_has_exact_hash_new_uuid_and_independent_empty_slots(case, monkeypatch):
    db, workspace, _ = case
    author(case)
    (workspace / "1234/assessment-review.yaml").unlink()
    reads = []

    def read_once(path):
        reads.append(path)
        return load_assessment(path)

    monkeypatch.setattr(scaffold_module, "load_assessment", read_once)
    result = scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    assert reads == [workspace / "1234/assessment-draft.yaml"]
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
    with closing(sqlite3.connect(db)) as connection, connection:
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


def _prior_and_latest(case):
    db, _workspace, _operation = case
    prior, review = pair_payload(review_number=12)
    first = AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-10-14T16:06:00+09:00")
    ).publish_reviewed_assessment(assessment_id(11), prior, review)
    latest, review = pair_payload(review_number=14)
    latest["findings"]["business_understanding"] += " Bで検討した補足。"
    latest["conclusion"]["assessed_at"] = "2026-10-14T16:08:00+09:00"
    review["reviewed_at"] = "2026-10-14T16:13:00+09:00"
    update_review_hash(latest, review)
    second = AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-10-14T16:16:00+09:00")
    ).publish_reviewed_assessment(
        assessment_id(13), latest, review, supersedes_id=first.assessment_id
    )
    return first, second


def test_refresh_from_old_material_supersedes_latest_and_publishes_new_reviewed_pair(case):
    db, workspace, _ = case
    prior, latest = _prior_and_latest(case)
    result = scaffold(case, from_assessment_id=prior.assessment_id)
    draft_path = Path(result["assessment_draft"])
    raw = yaml.safe_load(draft_path.read_text())
    identity = yaml.safe_load((draft_path.parent / "assessment-publication.yaml").read_text())
    assert raw["refresh"]["prior_assessment_ref"] == {
        "assessment_id": prior.assessment_id,
        "core_sha256": prior.core_sha256,
        "review_id": prior.review.review_id,
    }
    assert identity["supersedes_id"] == latest.assessment_id
    assert (
        raw["input_snapshot"]["sources"]
        == prior.document.model_dump(mode="json")["input_snapshot"]["sources"]
    )
    assert (
        raw["input_snapshot"]["facts"]
        == prior.document.model_dump(mode="json")["input_snapshot"]["facts"]
    )
    raw["valuation"]["horizon_months"] = 12
    for name in ("base", "downside"):
        raw["valuation"][name]["cash_distribution_per_share_yen"] = "0"
    raw["refresh"].update(
        checked_at=NOW.isoformat(),
        material_information_check="Aの保存資料と最新Bの差分を確認し、今回の所見を再検討した。",
        changed_sections=["findings"],
        unchanged_assumptions_reason="継続収益の仮定を原資料で再確認した。",
        horizon_reason="本日起点の約12ヶ月として再確認した。",
        distribution_reason="今回期間に受け取る分配0を再確認した。",
        corporate_action_reason="起点の株式単位に変更なし。",
    )
    raw["findings"]["value_gap_explanation"] += " 最新Bの補足を踏まえて原Aの価値差を再確認。"
    draft_path.write_text(yaml.safe_dump(raw))
    review_result = scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    review_path = Path(review_result["review_draft"])
    review = yaml.safe_load(review_path.read_text())
    review.update(
        reviewer_identity="synthetic-independent-refresh-reviewer",
        reviewed_at=(NOW + timedelta(minutes=1)).isoformat(),
        primary_source_check="verified",
        checked_source_ids=["annual", "price"],
        recalculated_projections=prior.review.model_dump(mode="json")["recalculated_projections"],
        strongest_countercase=raw["conclusion"]["strongest_countercase"],
        review_scope="Aの保存根拠と最新Bの補足を読み直し、今回の全coreと数値を独立検証した。",
    )
    review_path.write_text(yaml.safe_dump(review))
    promoted = promote(
        workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(minutes=2)
    )
    assert promoted.assessment_id == result["assessment_id"]
    from baibai_engine.storage.application.read import connect_read_only

    with closing(connect_read_only(db)) as connection, connection:
        pair = load_reviewed_assessment(connection, promoted.assessment_id)
        header = connection.execute(
            "SELECT supersedes_id,published_at FROM stock_assessment WHERE assessment_id=?",
            (promoted.assessment_id,),
        ).fetchone()
        assert pair.document.refresh.prior_assessment_ref.assessment_id == prior.assessment_id
        assert header[0] == latest.assessment_id
        assert datetime.fromisoformat(header[1]) > datetime.fromisoformat(
            "2026-10-14T16:16:00+09:00"
        )


def test_refresh_retry_preserves_identity_but_explicit_material_change_gets_new_identity(case):
    _db, _workspace, _operation = case
    prior, latest = _prior_and_latest(case)
    first = scaffold(case, from_assessment_id=prior.assessment_id)
    same = scaffold(
        case, now=NOW + timedelta(minutes=1), force=True, from_assessment_id=prior.assessment_id
    )
    assert same["assessment_id"] == first["assessment_id"]
    switched = scaffold(case, force=True, from_assessment_id=latest.assessment_id)
    assert switched["assessment_id"] != first["assessment_id"]
    raw = yaml.safe_load(Path(switched["assessment_draft"]).read_text())
    assert raw["refresh"]["prior_assessment_ref"]["assessment_id"] == latest.assessment_id
    identity = yaml.safe_load(
        (Path(switched["assessment_draft"]).parent / "assessment-publication.yaml").read_text()
    )
    assert identity["supersedes_id"] == latest.assessment_id


def test_refresh_file_failure_retry_preserves_scaffolded_identity(case, monkeypatch):
    _db, workspace, _operation = case
    prior, latest = _prior_and_latest(case)
    writer = scaffold_module.write_text_atomic

    def fail_draft(path, content):
        if path.name == "assessment-draft.yaml":
            raise OSError("synthetic draft write failure")
        writer(path, content)

    monkeypatch.setattr(scaffold_module, "write_text_atomic", fail_draft)
    with pytest.raises(OSError, match="draft write failure"):
        scaffold(case, from_assessment_id=prior.assessment_id)
    identity = yaml.safe_load((workspace / "1234/assessment-publication.yaml").read_text())
    assert not (workspace / "1234/assessment-draft.yaml").exists()
    monkeypatch.setattr(scaffold_module, "write_text_atomic", writer)
    resumed = scaffold(case, from_assessment_id=prior.assessment_id)
    assert resumed["assessment_id"] == identity["assessment_id"]
    assert identity["supersedes_id"] == latest.assessment_id


@pytest.mark.parametrize("completed", [False, True])
def test_published_holding_workspace_status_remains_readable_after_sale(tmp_path, completed):
    db = tmp_path / "app"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger)
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
    service = OperationService(db)
    operation = service.get(prepared.operation_id)
    progress = progress_binding(operation.payload)
    assert progress.outcomes[0].purpose == "holding"
    assert progress.outcomes[0].assessment_ref.assessment_id == published.assessment_id
    assert (
        compute_status(prepared.workspace, db_path=db, now=NOW)["workspace_status"] == "published"
    )
    from baibai_engine.position.store import LedgerStoreService

    assert LedgerStoreService(db).load().model_dump(mode="json") == before
    if completed:
        from tests.engine.test_research_progression import (
            final_payload,
            proposal_for,
            seed_proposal,
        )

        proposal = proposal_for(operation, actions={"2331": "hold"}).model_copy(
            update={"published_at": NOW + timedelta(seconds=1)}
        )
        seed_proposal(db, proposal)
        operation = service.record_proposal(
            prepared.operation_id, proposal.proposal_id, now=NOW + timedelta(seconds=1)
        )
        service.complete(
            prepared.operation_id,
            final_payload(operation),
            completed_at=NOW + timedelta(seconds=2),
        )

    from baibai_engine.position.drafts import apply_draft, build_sell_execution_draft

    sold_at = NOW + timedelta(days=1)
    ledger_service = LedgerStoreService(db)
    sale = build_sell_execution_draft(
        ledger_service,
        occurred_at=sold_at,
        ticker="2331",
        quantity=200,
        price_yen=Decimal("1100"),
    )
    assert sale is not None
    apply_draft(ledger_service, sale, human_confirmed=True)
    saved_files = {path: path.read_bytes() for path in prepared.workspace.rglob("*.yaml")}
    saved_database = db.read_bytes()

    parent = compute_status(prepared.workspace, db_path=db, now=sold_at)
    child_status = compute_status(child, db_path=db, now=sold_at)

    assert parent == child_status
    assert parent["workspace_status"] == "published"
    assert parent["cases"][0]["assessment_id"] == published.assessment_id
    assert db.read_bytes() == saved_database
    assert {path: path.read_bytes() for path in saved_files} == saved_files
    for write in (
        lambda: scaffold_assessment(
            workspace=child,
            ticker="2331",
            sqlite_path=tmp_path / "absent",
            as_of=sold_at.date(),
            retrieved_at=sold_at,
            db_path=db,
            force=True,
        ),
        lambda: scaffold_review(workspace=child, ticker="2331", db_path=db, force=True),
        lambda: promote(workspace=child, ticker="2331", db_path=db, now=sold_at),
    ):
        with pytest.raises(ResearchWorkspaceConflictError, match="current holding"):
            write()
    assert db.read_bytes() == saved_database
    assert {path: path.read_bytes() for path in saved_files} == saved_files


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


def test_review_scaffold_rejects_an_incomplete_assessment_without_writing_review(case):
    db, workspace, _ = case
    scaffold(case)
    with pytest.raises(AssessmentError):
        scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    assert not (workspace / "1234/assessment-review.yaml").exists()
