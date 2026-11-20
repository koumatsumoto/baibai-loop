"""元判断が必要な追記は空Appを作らず、新規開始と既存更新を区別する。"""

import json
import sqlite3
from datetime import timedelta

import pytest
import yaml
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli import investment_judgment, judgment_retrospective
from baibai_engine.investment_judgment.judgment_retrospective.query import scaffold_retrospective
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import selection_sha256


@pytest.mark.parametrize("kind", ["proposal", "retrospective"])
@pytest.mark.parametrize("state", ["missing_parent", "missing_file", "unwritten"])
def test_dependent_publication_does_not_create_the_missing_origin_store(
    tmp_path, capsys, kind, state
):
    case = backend_case(tmp_path)
    target = tmp_path / "other" / "app.sqlite"
    if state != "missing_parent":
        target.parent.mkdir()
    if state == "unwritten":
        target.touch()
    draft = tmp_path / "draft.yaml"
    if kind == "proposal":
        draft.write_text(yaml.safe_dump(reviewed_proposal(case).payload()))
        arguments = [
            "trade-proposal-publish",
            str(draft),
            "--db",
            str(target),
            "--sqlite-path",
            str(case.market),
        ]

        def run():
            return investment_judgment.main(arguments, now=PUBLISHED)
    else:
        raw = scaffold_retrospective(
            retrospective_id="synthetic-missing-origin-store",
            as_of=PUBLISHED.date(),
            milestone="合成の再訪",
            assessment_id=case.pair.assessment_id,
            db_path=case.app,
            known_at=PUBLISHED + timedelta(hours=1),
        )["draft"]
        raw["document"]["lesson"] = {"next_time": "元判断を再読する", "rationale": "合成の理由"}
        draft.write_text(yaml.safe_dump(raw))

        def run():
            return judgment_retrospective.main(
                ["save", str(draft), "--db", str(target)], now=PUBLISHED + timedelta(hours=1)
            )

    entries_before = set(tmp_path.rglob("*"))
    original = case.app.read_bytes()
    before = target.read_bytes() if target.exists() else None

    # Missing origin storage is an input-source error, not a published record.
    assert run() == 3

    captured = capsys.readouterr()
    if kind == "proposal":
        assert json.loads(captured.out)["status"] == "invalid_source"
        assert json.loads(captured.out)["operation_id"] is None
    else:
        assert captured.out == ""
        assert captured.err.startswith("error: ")
    assert (target.read_bytes() if target.exists() else None) == before
    assert set(tmp_path.rglob("*")) == entries_before
    assert case.app.read_bytes() == original


def test_checkpoint_uses_one_existing_writer_and_preserves_fixed_selection(tmp_path, monkeypatch):
    path = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=PUBLISHED, tickers=("1234",)))
    seed_historical_operation(path, operation)
    service = OperationService(path)
    updated = operation.payload.model_copy(update={"checkpoint": "一次資料の確認を再開"})
    connect = sqlite3.connect
    opened = []

    def track(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        opened.append((str(database), connection))
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", track)
        result = service.checkpoint(operation.operation_id, note=updated.checkpoint, now=PUBLISHED)

    assert len(opened) == 2
    assert "mode=ro" in opened[0][0]
    assert "mode=rw" in opened[1][0]
    assert result.payload == updated
    assert result.started_at == operation.started_at
    assert selection_sha256(selection_binding(result.payload)) == selection_sha256(
        selection_binding(operation.payload)
    )
    assert service.get(operation.operation_id) == result
    for _, connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
