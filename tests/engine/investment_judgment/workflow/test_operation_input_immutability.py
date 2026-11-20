"""固定生成の数値型と保存Selectionを、実CLIの書込境界で保つ。"""

from datetime import datetime

import pytest
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli.operation import main
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _generated(tmp_path):
    return seed_generated_selection(
        runs=tmp_path / "runs",
        now=NOW,
        tickers=("1234",),
        models=(
            GeneratedModel(
                model_id="technical_analysis",
                subject="1234",
                calculation_revision="synthetic-v1",
                payload={"sample": 0, "reason": "合成の型境界"},
            ),
        ),
    )


@pytest.mark.parametrize("value", [False, 0.0, None, 0])
def test_start_preserves_exact_generated_payload_type(tmp_path, capsys, value):
    selected = _generated(tmp_path)
    raw = selected.model_dump(mode="json")
    raw["inputs"]["generation"]["models"][0]["payload"]["sample"] = value
    app, runs = tmp_path / "app", tmp_path / "runs"
    source_before = runs.read_bytes()
    draft = generated_draft_for(selected).model_copy(
        update={"expected_selection_sha256": selection_sha256(parse_selection(raw))}
    )
    service = OperationService(app, runs_path=runs)
    if type(value) is not int:
        with pytest.raises(ValueError, match="supplied hash"):
            service.start(draft, now=NOW)
        assert service.active() is None
    else:
        _, saved = service.start(draft, now=NOW)
        actual = saved.inputs.generation.models[0].payload["sample"]
        assert type(actual) is int
        assert actual == 0
    assert runs.read_bytes() == source_before


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_checkpoint_preserves_saved_selection_json_and_hash(tmp_path, version):
    if version == 2:
        raw = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
        raw["inputs"]["market_context"]["sample"] = 0
    else:
        from tests.helpers.generated_selection import legacy_selection_payload

        selected = _generated(tmp_path)
        raw = (
            selected.model_dump(mode="json")
            if version == 5
            else legacy_selection_payload(selected, tmp_path / "runs", version=version)
        )
    selected = parse_selection(raw)
    operation = historical_operation(selected)
    app = tmp_path / "app"
    seed_historical_operation(app, operation)
    service = OperationService(app)
    operation = service.get(operation.operation_id)
    assert (
        main(
            ["--db", str(app), "checkpoint", operation.operation_id, "--note", "new note"], now=NOW
        )
        == 0
    )
    after = service.get(operation.operation_id)
    assert after.payload.artifacts == operation.payload.artifacts
    assert selection_sha256(selection_binding(after.payload)) == selection_sha256(selected)
