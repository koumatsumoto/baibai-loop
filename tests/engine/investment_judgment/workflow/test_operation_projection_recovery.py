"""DB保存後に壊れた投影を読んでも、保存IDで同じ作業を回復する。"""

import json
from datetime import datetime

import pytest
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.workflow.operation.service import OperationService

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize("broken_inputs", ["not: [valid\n", "- not-a-mapping\n"])
def test_projection_read_error_returns_saved_id_and_force_resume_keeps_draft(
    tmp_path, capsys, monkeypatch, broken_inputs
):
    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "workspace"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(generated_draft_for(selected).model_dump_json())
    authored = workspace / "1234" / "assessment-draft.yaml"
    authored.parent.mkdir(parents=True)
    authored.write_text("authored draft stays byte-for-byte\n")
    projection = workspace / "domain-inputs.yaml"
    projection.write_text(broken_inputs)
    arguments = ["prepare", "--db", str(app), "--workspace", str(workspace)]
    assert (
        main(
            [*arguments, "--selection-file", str(selection_path), "--runs-path", str(runs)], now=NOW
        )
        == 4
    )
    error = capsys.readouterr().err
    operation = OperationService(app).active()
    assert operation is not None
    assert f"Operation {operation.operation_id} is saved; resume its projection" in error
    assert not (workspace / "manifest.yaml").exists()
    assert projection.read_text() == broken_inputs
    before = app.read_bytes(), authored.read_bytes()
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.workspace.selected_prepare.read_research_model_inputs",
        lambda *_, **__: pytest.fail("saved Operation reread the model generation"),
    )
    assert main([*arguments, "--operation-id", operation.operation_id, "--force"], now=NOW) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["operation_id"] == operation.operation_id
    assert resumed["status"] == "resumed"
    assert (app.read_bytes(), authored.read_bytes()) == before
    assert (workspace / "manifest.yaml").exists()
    assert OperationService(app).get(operation.operation_id) == operation


def test_input_read_error_before_commit_still_has_no_operation_id(tmp_path, capsys, monkeypatch):
    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "workspace"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(generated_draft_for(selected).model_dump_json())

    def fail(*_, **__):
        raise OSError("required input unavailable")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.workspace.selected_prepare.read_research_model_inputs",
        fail,
    )
    assert (
        main(
            [
                "prepare",
                "--db",
                str(app),
                "--workspace",
                str(workspace),
                "--selection-file",
                str(selection_path),
                "--runs-path",
                str(runs),
            ],
            now=NOW,
        )
        == 3
    )
    error = json.loads(capsys.readouterr().out)
    assert error["operation_id"] is None
    assert error["reason"] == "required input unavailable"
    assert not app.exists()
    assert not workspace.exists()
