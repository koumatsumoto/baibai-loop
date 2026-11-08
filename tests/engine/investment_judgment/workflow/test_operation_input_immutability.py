"""固定生成の数値型と保存Selectionを、実CLIの書込境界で保つ。"""

import json
from datetime import datetime

import pytest
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli.operation import main
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
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
    payload = tmp_path / "payload.json"
    payload.write_text(OperationPayload(checkpoint="start", artifacts=(raw,)).model_dump_json())
    app, runs = tmp_path / "app", tmp_path / "runs"
    source_before = runs.read_bytes()
    result = main(
        [
            "--db",
            str(app),
            "--runs-path",
            str(runs),
            "--format",
            "json",
            "start",
            "--kind",
            "capital-allocation",
            "--as-of",
            NOW.date().isoformat(),
            "--payload",
            str(payload),
        ],
        now=NOW,
    )
    if type(value) is not int:
        assert result == 1
        assert "model input slice differs from its canonical generation" in capsys.readouterr().err
        assert not app.exists()
    else:
        assert result == 0
        saved = selection_binding(OperationService(app).active().payload)
        actual = saved.inputs.generation.models[0].payload["sample"]
        assert type(actual) is int
        assert actual == 0
    assert runs.read_bytes() == source_before


@pytest.mark.parametrize("version", [2, 3, 4])
@pytest.mark.parametrize("change", ["false", "float", "null", "reordered", "same"])
def test_checkpoint_preserves_saved_selection_json_and_hash(tmp_path, capsys, version, change):
    if version == 2:
        raw = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
        raw["inputs"]["market_context"]["sample"] = 0
    else:
        raw = _generated(tmp_path).model_dump(mode="json")
        if version == 3:
            raw["schema_version"] = 3
            del raw["inputs"]["saved_company_context"]
    selected = parse_selection(raw)
    operation = historical_operation(selected)
    app = tmp_path / "app"
    seed_historical_operation(app, operation)
    service = OperationService(app)
    operation = service.get(operation.operation_id)
    payload = operation.payload.model_dump(mode="json")
    target = (
        payload["artifacts"][0]["inputs"]["market_context"]
        if version == 2
        else payload["artifacts"][0]["inputs"]["generation"]["models"][0]["payload"]
    )
    if change in {"false", "float", "null"}:
        target["sample"] = {"false": False, "float": 0.0, "null": None}[change]
    if change == "reordered":
        payload["artifacts"][0] = dict(reversed(tuple(payload["artifacts"][0].items())))
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(payload))
    before = app.read_bytes()
    result = main(
        [
            "--db",
            str(app),
            "--format",
            "json",
            "checkpoint",
            operation.operation_id,
            "--payload",
            str(path),
        ],
        now=NOW,
    )
    if change in {"false", "float", "null"}:
        assert result == 1
        assert "Research selection is immutable" in capsys.readouterr().err
        assert app.read_bytes() == before
    else:
        assert result == 0
    after = service.get(operation.operation_id)
    assert after.payload == operation.payload
    assert selection_sha256(selection_binding(after.payload)) == selection_sha256(selected)
