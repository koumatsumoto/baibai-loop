"""保存政策カードのJSON型を、同IDの実Operation開始で差し替えない。"""

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.workflow.operation import service as operation_service
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
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
def test_start_preserves_published_screening_json_types(tmp_path, method, change):
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

    def admit():
        return OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(checkpoint="start", artifacts=(raw,)),
        )

    source_before = runs.read_bytes()
    if change in {"canonical", "reordered"}:
        saved = selection_binding(admit().payload)
        assert canonical_json(saved.model_dump(mode="json")) == canonical_json(original)
        policy = saved.inputs.screening_selection
        assert policy.universe_tickers == ("1234", "5678")
        assert policy.result.tickers == ("1234",)
        assert policy.result.policy.method == method
        assert {row.subject for row in saved.inputs.generation.models} == {"1234", "5678"}
    else:
        with pytest.raises(ValueError, match="screening selection differs from chosen generation"):
            admit()
        assert not app.exists()
    assert runs.read_bytes() == source_before


@pytest.mark.parametrize("legacy_format", [False, True])
def test_finite_case_is_bound_to_the_actual_published_bytes(tmp_path, legacy_format):
    from baibai_engine.investment_judgment.workflow.case_screening import read_case_screening

    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    service = OperationService(app, runs_path=runs)

    def start(record):
        return service.start(
            session_kind="capital-allocation",
            as_of=record.research_as_of,
            started_at=NOW,
            payload=OperationPayload(
                checkpoint="start", artifacts=(record.model_dump(mode="json"),)
            ),
        )

    if legacy_format:
        with sqlite3.connect(runs) as connection:
            (blob,) = connection.execute("SELECT payload_gzip FROM screening_selection").fetchone()
            wire = json.dumps(json.loads(gzip.decompress(blob)), indent=2).encode()
            connection.execute(
                "UPDATE screening_selection SET payload_gzip=?", (gzip.compress(wire, mtime=0),)
            )
        from baibai_engine.investment_judgment.workflow.operation.service import (
            OperationConflictError,
        )

        with pytest.raises(OperationConflictError, match="screening selection differs"):
            start(selected)
        assert not app.exists()
        _, fresh = read_case_screening(
            selected.inputs.screening_selection_id, path=runs, known_at=NOW, subjects=("1234",)
        )
        selected = selected.model_copy(
            update={"inputs": selected.inputs.model_copy(update={"screening_selection": fresh})}
        )
    before = runs.read_bytes()
    assert selection_binding(start(selected).payload) == selected
    assert runs.read_bytes() == before


@pytest.mark.parametrize(
    "case", ["other", "other_corrupt", "own_corrupt", "future_other", "absent"]
)
def test_start_checks_visible_selection_owner_before_reading_its_body(tmp_path, monkeypatch, case):
    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    other = seed_generated_selection(runs=runs, now=NOW, tickers=("5678",))
    assert selected.inputs.generation.generation_id != other.inputs.generation.generation_id
    key = (
        selected.inputs.screening_selection_id
        if case == "own_corrupt"
        else ("not-published" if case == "absent" else other.inputs.screening_selection_id)
    )
    if case in {"other_corrupt", "own_corrupt", "future_other"}:
        published_at = NOW + timedelta(seconds=1) if case == "future_other" else NOW
        with closing(sqlite3.connect(runs)) as connection, connection:
            connection.execute(
                "UPDATE screening_selection SET payload_gzip=?,published_at=? WHERE selection_id=?",
                (b"broken body", published_at.isoformat(), key),
            )
    saved = selected.model_dump(mode="json")
    saved["inputs"]["screening_selection_id"] = key
    reads = set()
    original = operation_service.connect_runs_read_only

    def traced(path):
        connection = original(path)

        def observe(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ:
                reads.add((table, column))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(observe)
        return connection

    monkeypatch.setattr(operation_service, "connect_runs_read_only", traced)
    error = gzip.BadGzipFile if case == "own_corrupt" else operation_service.OperationConflictError
    message = "Not a gzipped file" if case == "own_corrupt" else "screening selection differs"
    with pytest.raises(error, match=message):
        OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=selected.research_as_of,
            started_at=NOW,
            payload=OperationPayload(checkpoint="start", artifacts=(saved,)),
        )
    assert ("screening_selection", "selection_id") in reads
    assert (("screening_selection", "payload_gzip") in reads) == (case == "own_corrupt")
    assert not app.exists()
