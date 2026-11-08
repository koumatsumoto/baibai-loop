"""合成の保存判断だけを使い、未選定・見送り・未完了と時点を混同しない。"""

import json
import socket
import sqlite3
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

import pytest
from tests.helpers.assessment_v1 import NOW, assessment_id, pair_payload
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.operation_proposal import PUBLISHED, proposal_for, seed_proposal
from tests.helpers.research_selection import historical_operation, selection
from tools.experiments.inspect_decision_cohort import inspect_decision_cohort, main

from baibai_engine.investment_judgment.stock_assessment.store import publish_reviewed_assessment
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import GeneratedResearchSelectionV4
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


def _assessment(db, *, number=1, now=NOW, supersedes_id=None):
    document, review = pair_payload(review_number=number + 100)
    with closing(connect_rw(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        publish_reviewed_assessment(
            connection,
            assessment_id(number),
            document,
            review,
            published_at=now,
            supersedes_id=supersedes_id,
        )
    return assessment_id(number)


def _start(tmp_path, tickers=("1234", "5678")):
    db, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    initialize_database(db)
    universe = ("1234", "5678", "9012", "3456")
    result = select_economic_paths(
        [
            CompanyCoordinates(
                ticker=ticker,
                name="合成企業",
                sector="合成業種",
                paths=(
                    PathCoordinates(
                        "earning_power", "synthetic", value, value, "price", "business", True, {}
                    ),
                ),
                context={},
                model_revisions={},
                not_generated=(),
            )
            for value, ticker in zip((4, 3, 2, 1), universe, strict=True)
        ],
        policy=EconomicPathPolicy(candidate_limit=3),
    )
    full = seed_generated_selection(runs=runs, db=db, now=NOW, tickers=tickers)
    identity = "synthetic-complete-universe"
    ScreeningSelectionStore(runs).publish(
        identity, ScreeningSelection(full.inputs.generation.generation_id, universe, result)
    )
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
            ((NOW - timedelta(seconds=30)).isoformat(), identity),
        )
    publication = read_publication(path=runs, selection_id=identity)
    inputs = full.inputs.model_copy(
        update={
            "screening_selection_id": identity,
            "screening_selection": publication.selection,
            "screening_published_at": publication.published_at,
        }
    )
    selected = GeneratedResearchSelectionV4.model_validate(
        full.model_copy(
            update={
                "targets": tuple(t for t in full.targets if t.ticker in tickers),
                "inputs": inputs,
            }
        ).model_dump(mode="json")
    )
    service = OperationService(db, runs_path=runs)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        payload=OperationPayload(
            checkpoint="合成調査", artifacts=(selected.model_dump(mode="json"),)
        ),
    )
    return db, runs, service, operation


def _finish(service, operation):
    return service.complete(
        operation.operation_id,
        result="合成の調査結果",
        next="必要な事実を後日確認する",
        completed_at=PUBLISHED + timedelta(minutes=1),
    )


def _unfinished(service, operation, ticker):
    return service.record_unfinished(
        operation.operation_id,
        ticker=ticker,
        reason="原典の期間対応が確認できない",
        references=("https://example.invalid/primary",),
        now=NOW,
    )


@pytest.mark.parametrize("action", ["no_action", "buy"])
def test_exact_baseline_keeps_candidates_selection_actions_and_unfinished_distinct(
    tmp_path, monkeypatch, action
):
    db, runs, service, operation = _start(tmp_path)
    reference = _assessment(db)
    service.record_assessment(operation.operation_id, reference, now=NOW)
    operation = _unfinished(service, operation, "5678")
    proposal = proposal_for(operation, actions={"1234": action})
    seed_proposal(db, proposal)
    service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    completed = _finish(service, operation)
    # 固定Selectionだけで読む。現在generation・marketや通信に依存しない。
    runs.unlink()
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: pytest.fail("network access"))
    before = db.read_bytes()
    observed = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)
    assert db.read_bytes() == before
    baseline = observed["baseline"]
    assert baseline["baseline_available_at"] == completed.completed_at.isoformat()
    assert baseline["operation_started_at"] == NOW.isoformat()
    assert baseline["screening_published_at"] == (NOW - timedelta(seconds=30)).isoformat()
    assert baseline["proposal_published_at"] == PUBLISHED.isoformat()
    assert baseline["input_known_at"] == NOW.isoformat()
    assert set(baseline["screening_universe_tickers"]) == {"1234", "5678", "9012", "3456"}
    assert baseline["row_scope"] == "screening_candidates_union_selected_targets"
    assert "other_operations_including_unfinished_or_abandoned" in baseline["not_measured"]
    rows = {row["ticker"]: row for row in baseline["rows"]}
    assert set(rows) == {"1234", "5678", "9012"}
    assert rows["1234"]["proposal_item"]["action"] == action
    assert rows["1234"]["research_status"] == "reviewed"
    assert rows["5678"]["proposal_item"]["action"] == "defer"
    assert rows["5678"]["research_status"] == "unfinished"
    assert rows["9012"]["research_status"] == "not_selected"
    assert rows["9012"]["proposal_item"] is None
    assert rows["9012"]["research_target"] is None
    assert rows["1234"]["retrospective_origin"]["proposal_id"] == proposal.proposal_id
    assert observed["baseline_sha256"] == sha256(canonical_json(baseline).encode()).hexdigest()
    # 後日の同ticker評価も、このcohortの参照と内容を置換しない。
    _assessment(db, number=2, now=PUBLISHED + timedelta(days=1), supersedes_id=reference)
    assert inspect_decision_cohort(db_path=db, operation_id=completed.operation_id) == observed


def test_selected_outside_candidates_is_not_silently_dropped(tmp_path):
    db, _runs, service, operation = _start(tmp_path, tickers=("3456",))
    _unfinished(service, operation, "3456")
    completed = _finish(service, operation)
    baseline = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)["baseline"]
    row = next(row for row in baseline["rows"] if row["ticker"] == "3456")
    assert row["candidate"] is None
    assert row["research_target"]["ticker"] == "3456"
    assert row["research_status"] == "unfinished"
    assert row["proposal_item"] is None
    assert row["in_screening_universe"] is True


def test_empty_selection_retains_candidates_without_fabricating_a_proposal(tmp_path):
    db, _runs, service, operation = _start(tmp_path, tickers=())
    completed = _finish(service, operation)
    baseline = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)["baseline"]
    assert len(baseline["rows"]) == 3
    assert all(row["research_status"] == "not_selected" for row in baseline["rows"])
    assert baseline["proposal_ref"] is None
    assert baseline["not_measured"]


def test_active_operation_is_not_a_completed_decision_baseline(tmp_path):
    db, _runs, _service, operation = _start(tmp_path)
    with pytest.raises(ValueError, match="completed immutable"):
        inspect_decision_cohort(db_path=db, operation_id=operation.operation_id)


def test_legacy_selection_is_not_current_economic_policy_evidence(tmp_path):
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW)).model_copy(
        update={"status": "completed", "completed_at": PUBLISHED}
    )
    seed_historical_operation(db, operation)
    with pytest.raises(ValueError, match="generated-model"):
        inspect_decision_cohort(db_path=db, operation_id=operation.operation_id)


def test_corrupt_proposal_reference_is_not_replaced_with_latest(tmp_path):
    db, _runs, service, operation = _start(tmp_path, tickers=("1234",))
    reference = _assessment(db)
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    completed = _finish(service, operation)
    with closing(sqlite3.connect(db)) as connection, connection:
        # 合成DBだけを故意に破損させ、通常のimmutable writerを代用しない。
        connection.execute("DROP TRIGGER investment_proposal_immutable")
        connection.execute("UPDATE investment_proposal SET draft_sha256=?", ("0" * 64,))
    with pytest.raises(ValueError, match="identity/hash"):
        inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)


def test_cli_private_output_is_create_only_and_missing_db_is_not_created(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite"
    assert main(["--db", str(missing), "--operation-id", "research-op-" + "0" * 32]) == 2
    assert not missing.exists()
    assert "OperationNotFoundError" in capsys.readouterr().err
    db, _runs, service, operation = _start(tmp_path, tickers=())
    completed = _finish(service, operation)
    output = tmp_path / "private.json"
    argv = ["--db", str(db), "--operation-id", completed.operation_id, "--out", str(output)]
    assert main(argv) == 0
    assert capsys.readouterr().out == ""
    saved = output.read_bytes()
    assert json.loads(saved)["baseline"]["operation_id"] == completed.operation_id
    assert main(argv) == 2
    assert output.read_bytes() == saved
    assert "FileExistsError" in capsys.readouterr().err


@pytest.mark.parametrize("fault", ["private_payload", "obsolete_schema"])
def test_cli_expected_failures_do_not_log_private_values(tmp_path, capsys, fault):
    db, _runs, service, operation = _start(tmp_path, tickers=())
    completed = _finish(service, operation)
    with closing(sqlite3.connect(db)) as connection, connection:
        if fault == "private_payload":
            # 合成DBだけの破損。Pydanticの例外本文に私有値が入るcaseを再現する。
            connection.execute("DROP TRIGGER operation_session_completed_no_update")
            payload = completed.payload.model_dump(mode="json")
            payload["checkpoint"] = {"private": "PRIVATECANARY"}
            connection.execute(
                "UPDATE operation_session SET payload=? WHERE operation_id=?",
                (json.dumps(payload), completed.operation_id),
            )
        else:
            connection.execute("PRAGMA user_version=1000000")
    before = db.read_bytes()
    output = tmp_path / "private.json"
    assert (
        main(["--db", str(db), "--operation-id", completed.operation_id, "--out", str(output)]) == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PRIVATECANARY" not in captured.err
    assert completed.operation_id not in captured.err
    assert str(db) not in captured.err
    expected_class = "ValidationError" if fault == "private_payload" else "RuntimeError"
    assert captured.err == f"error: decision cohort inspection failed ({expected_class})\n"
    assert not output.exists()
    assert db.read_bytes() == before


@pytest.mark.parametrize("generation_version", [1, 2])
@pytest.mark.parametrize("selection_version", [3, 4])
def test_migrated_cohort_distinguishes_publication_from_operational_rows(
    tmp_path, monkeypatch, generation_version, selection_version
):
    from tests.engine.investment_judgment.workflow.test_selection_evidence import (
        WRONG,
        evidence_case,
    )

    from baibai_engine.investment_judgment.workflow.selection import selection_sha256

    case, original, operational, published = evidence_case(
        tmp_path,
        monkeypatch,
        generation_version=generation_version,
        selection_version=selection_version,
    )
    before = case.app.read_bytes()
    baseline = inspect_decision_cohort(db_path=case.app, operation_id=case.operation.operation_id)[
        "baseline"
    ]
    assert case.app.read_bytes() == before
    assert baseline["selection_role"] == "operational"
    assert baseline["selection_sha256"] == baseline["operational_selection_sha256"]
    assert baseline["operational_selection_sha256"] == selection_sha256(operational)
    assert baseline["published_selection_sha256"] == sha256(published.encode()).hexdigest()
    assert baseline["published_selection_sha256"] == original.research_scope.selection_sha256
    assert baseline["published_selection_sha256"] != baseline["operational_selection_sha256"]
    assert baseline["publication_evidence_role"] == "as_published"
    # 後日の訂正先やlatestではなく、完了Operationが記録した提案を保つ。
    assert baseline["proposal_ref"]["proposal_id"] == WRONG
    assert baseline["rows"][0]["retrospective_origin"]["proposal_id"] == WRONG
    assert baseline["row_scope"] == "screening_candidates_union_selected_targets"
    assert [row["ticker"] for row in baseline["rows"]] == ["1234"]
    assert baseline["rows"][0]["candidate"]["model_revisions"] == {
        "company_financials": "original-calculation"
    }
    assert "human_adoption_or_execution" in baseline["not_measured"]
    assert "forward_returns_or_policy_effectiveness" in baseline["not_measured"]


@pytest.mark.parametrize("damage", ["operational_hash", "canonical", "no_receipt"])
def test_migrated_cohort_rejects_missing_or_damaged_evidence(tmp_path, monkeypatch, damage):
    from tests.engine.investment_judgment.workflow.test_selection_evidence import evidence_case

    case, _, _, _ = evidence_case(tmp_path, monkeypatch, damage=damage)
    before = case.app.read_bytes()
    with pytest.raises(ValueError, match=r"exact inputs|exact published"):
        inspect_decision_cohort(db_path=case.app, operation_id=case.operation.operation_id)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("field", ["operation_id", "selection_sha256"])
def test_migrated_cohort_rejects_a_resigned_foreign_scope(tmp_path, monkeypatch, field):
    from tests.engine.investment_judgment.workflow.test_selection_evidence import (
        WRONG,
        evidence_case,
    )

    from baibai_engine.investment_judgment.trade_proposal.contracts import (
        TradeProposal,
        proposal_draft_sha256,
    )

    case, _, _, _ = evidence_case(tmp_path, monkeypatch)
    with sqlite3.connect(case.app) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT payload FROM investment_proposal WHERE proposal_id=?", (WRONG,)
            ).fetchone()[0]
        )
        raw["research_scope"][field] = (
            "research-op-" + "0" * 32 if field == "operation_id" else "f" * 64
        )
        digest = proposal_draft_sha256(TradeProposal.model_validate(raw))
        raw["review"]["draft_sha256"] = digest
        # 合成DBの署名まで整合させ、単なる署名破損でなくscope境界を試験する。
        connection.execute("DROP TRIGGER investment_proposal_immutable")
        connection.execute(
            "UPDATE investment_proposal SET payload=?,draft_sha256=? WHERE proposal_id=?",
            (canonical_json(raw), digest, WRONG),
        )
        connection.execute("DROP TRIGGER operation_session_completed_no_update")
        payload = json.loads(
            connection.execute("SELECT payload FROM operation_session").fetchone()[0]
        )
        for artifact in payload["artifacts"]:
            if artifact.get("kind") == "research_progress":
                artifact["proposal_ref"]["draft_sha256"] = digest
        connection.execute("UPDATE operation_session SET payload=?", (canonical_json(payload),))
    before = case.app.read_bytes()
    with pytest.raises(ValueError, match=r"proposal scope differs|exact published"):
        inspect_decision_cohort(db_path=case.app, operation_id=case.operation.operation_id)
    assert case.app.read_bytes() == before
