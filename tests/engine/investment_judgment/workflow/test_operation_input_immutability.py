"""固定生成の数値型と保存Selectionを、実CLIの書込境界で保つ。"""

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
from baibai_engine.primitives.json import encode_json

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
def test_start_preserves_exact_generated_payload_type(tmp_path, value):
    selected = _generated(tmp_path)
    raw = selected.model_dump(mode="json")
    raw["inputs"]["generation"]["models"][0]["payload"]["sample"] = value
    app, runs = tmp_path / "app", tmp_path / "runs"

    def admit():
        return OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(checkpoint="start", artifacts=(raw,)),
        )

    source_before = runs.read_bytes()
    if type(value) is not int:
        with pytest.raises(
            ValueError, match="model input slice differs from its canonical generation"
        ):
            admit()
        assert not app.exists()
    else:
        saved = selection_binding(admit().payload)
        actual = saved.inputs.generation.models[0].payload["sample"]
        assert type(actual) is int
        assert actual == 0
    assert runs.read_bytes() == source_before


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_checkpoint_preserves_saved_selection_progress_and_extra_opaque_bytes(tmp_path, version):
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
    original = historical_operation(selected)
    original = original.model_copy(
        update={
            "payload": original.payload.model_copy(
                update={
                    "canonical_refs": ("fixed-original",),
                    "result": "old working result",
                    "next": "keep original next action",
                    "artifacts": (
                        raw,
                        {
                            "kind": "research_progress",
                            # Older serialized shape: no proposal_ref key, and not the last artifact.
                            "outcomes": [
                                {
                                    "status": "unfinished",
                                    "ticker": "1234",
                                    "purpose": "new",
                                    "unfinished_reason": "evidence missing",
                                    "references": ["original"],
                                }
                            ],
                        },
                        {"kind": "opaque", "future": [False, 0, 0.0, None]},
                    ),
                }
            )
        }
    )
    app = tmp_path / "app"
    seed_historical_operation(app, original)
    before = original.payload.model_dump(mode="json")
    code = main(
        [
            "--db",
            str(app),
            "--format",
            "json",
            "checkpoint",
            original.operation_id,
            "--note",
            "a separate new note",
        ],
        now=NOW,
    )
    assert code == 0
    after = OperationService(app).get(original.operation_id)
    assert encode_json(after.payload.model_dump(mode="json")) == encode_json(
        {**before, "checkpoint": "a separate new note"}
    )
    assert "proposal_ref" not in after.payload.artifacts[1]
    assert selection_sha256(selection_binding(after.payload)) == selection_sha256(selected)
