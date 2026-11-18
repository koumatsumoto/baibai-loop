"""保存政策カードのJSON型を、同IDの実Operation開始で差し替えない。"""

import gzip
import json
import sqlite3
from datetime import datetime

import pytest
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.cli.operation import main
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening import selection_query
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CandidateCard,
    EconomicPathPolicy,
    EconomicPathSelection,
    PathNormalization,
    RankedPath,
)
from baibai_engine.primitives.json import canonical_json

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize(
    "method", ["best-economic-path-v1", "best-economic-path-v2", "best-economic-path-v4"]
)
@pytest.mark.parametrize(
    "change",
    [
        "context_zero_bool",
        "context_true_int",
        "evidence_zero_bool",
        "evidence_true_int",
        "evidence_zero_float",
        "context_null",
        "canonical",
        "reordered",
    ],
)
def test_start_preserves_published_screening_json_types(tmp_path, capsys, method, change):
    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(
        runs=runs,
        now=NOW,
        tickers=("1234", "5678"),
        models=tuple(
            GeneratedModel(
                model_id="technical_analysis",
                subject=ticker,
                calculation_revision="synthetic-v1",
                payload={"sample": 0},
            )
            for ticker in ("1234", "5678")
        ),
        screening_result=EconomicPathSelection(
            EconomicPathPolicy(method=method),
            1,
            (
                CandidateCard(
                    ticker="1234",
                    name="合成企業",
                    sector=None,
                    rank=1,
                    priority=0.5,
                    strongest_path="earning_power",
                    paths=(
                        RankedPath(
                            "earning_power",
                            "annual_ttm",
                            0.1,
                            0.25,
                            "market",
                            "sector",
                            0.5,
                            0.5,
                            1,
                            1,
                            0.5,
                            {"sample": 0, "flag": True},
                            PathNormalization(0.5, 1, 1.0)
                            if method == "best-economic-path-v4"
                            else None,
                        ),
                    ),
                    context={"sample": 0, "flag": True},
                    model_revisions={"technical_analysis": "synthetic-v1"},
                    not_generated=(),
                ),
            ),
        ),
    )
    original = selected.model_dump(mode="json")
    raw = selected.model_dump(mode="json")
    screening = raw["inputs"]["screening_selection"]
    card = screening["result"]["cards"][0]
    if change.startswith("context_"):
        key, value = {
            "context_zero_bool": ("sample", False),
            "context_true_int": ("flag", 1),
            "context_null": ("sample", None),
        }[change]
        card["context"][key] = value
    elif change.startswith("evidence_"):
        key, value = {
            "evidence_zero_bool": ("sample", False),
            "evidence_true_int": ("flag", 1),
            "evidence_zero_float": ("sample", 0.0),
        }[change]
        card["paths"][0]["evidence"][key] = value
    elif change == "reordered":
        card["context"] = dict(reversed(tuple(card["context"].items())))
        card["paths"][0]["evidence"] = dict(reversed(tuple(card["paths"][0]["evidence"].items())))
        screening["result"] = dict(reversed(tuple(screening["result"].items())))
        raw["inputs"]["screening_selection"] = dict(reversed(tuple(screening.items())))
    payload = tmp_path / "payload.json"
    payload.write_text(OperationPayload(checkpoint="start", artifacts=(raw,)).model_dump_json())
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
    if change in {"canonical", "reordered"}:
        assert result == 0
        output = json.loads(capsys.readouterr().out)
        saved = selection_binding(OperationService(app).get(output["operation_id"]).payload)
        assert canonical_json(saved.model_dump(mode="json")) == canonical_json(original)
        policy = saved.inputs.screening_selection
        assert policy.universe_tickers == ("1234", "5678")
        assert policy.result.tickers == ("1234",)
        assert policy.result.policy.method == method
        assert {row.subject for row in saved.inputs.generation.models} == {"1234", "5678"}
    else:
        assert result == 1
        assert "screening selection differs from chosen generation" in capsys.readouterr().err
        assert not app.exists()
    assert runs.read_bytes() == source_before


@pytest.mark.parametrize("legacy_format", [False, True])
def test_start_reads_typed_publication_only_for_noncanonical_saved_bytes(
    tmp_path, monkeypatch, legacy_format
):
    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    payload = OperationPayload(checkpoint="start", artifacts=(selected.model_dump(mode="json"),))
    if legacy_format:
        with sqlite3.connect(runs) as connection:
            (blob,) = connection.execute("SELECT payload_gzip FROM screening_selection").fetchone()
            raw = json.loads(gzip.decompress(blob))
            connection.execute(
                "UPDATE screening_selection SET payload_gzip=?",
                (gzip.compress(json.dumps(raw, indent=2).encode(), mtime=0),),
            )
    source_before = runs.read_bytes()
    decode = selection_query._decode_selection
    reads = []

    def observed(raw):
        reads.append(len(raw))
        return decode(raw)

    monkeypatch.setattr(selection_query, "_decode_selection", observed)
    operation = OperationService(app, runs_path=runs).start(
        session_kind="capital-allocation",
        as_of=selected.research_as_of,
        started_at=NOW,
        payload=payload,
    )
    assert len(reads) == int(legacy_format)
    assert operation.payload == payload
    assert runs.read_bytes() == source_before
