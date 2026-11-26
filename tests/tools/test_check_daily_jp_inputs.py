"""One native selection and quote on runner-local copies; stdout never exposes the input."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from tests.helpers.model_generation import publish_generation
from tests.helpers.screening_sqlite import seed_daily_bars
from tests.helpers.technical_discovery import _metadata, _models
from tools.diagnostics.check_daily_jp_inputs import main

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGeneration
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
)


@pytest.mark.parametrize(
    ("test_case", "result", "stage"),
    [
        ("exact", 0, "PASS:"),
        ("other generation", 1, "selection-context"),
        ("outside universe", 1, "selection-context"),
        ("previous close", 1, "unadjusted close"),
        ("other run", 1, "daily inputs"),
        ("other workflow", 1, "daily inputs"),
        ("models failed", 1, "daily inputs"),
        ("wrong local origin", 1, "daily inputs"),
        ("other market digest", 1, "selection-context"),
    ],
)
def test_local_daily_input_probe(tmp_path: Path, monkeypatch, capsys, test_case, result, stage):
    market, runs = tmp_path / "market.sqlite", tmp_path / "runs.sqlite"
    meta, models = _metadata(), _models()
    origin = LakeStoreOrigin("daily-test", "a" * 64)
    meta = meta.model_copy(
        update={
            "inputs": {
                **meta.inputs,
                "market": InputSnapshot(
                    kind="lake_release",
                    reference=origin.release_id,
                    sha256="b" * 64
                    if test_case == "other market digest"
                    else origin.release_manifest_sha256,
                ),
            }
        }
    )
    publish_generation(ModelGeneration(**dict(meta), models=models), path=runs)
    row = project_discovery_row("1234", models=models, metadata=meta)
    selection = evaluate_discovery_rows(meta, tickers=("1234", "5678"), rows={"1234": row})
    identity = discovery_publication_id(meta.generation_id)
    ScreeningSelectionStore(runs).publish(identity, selection)
    # Synthetic model date is a Saturday. Its real expected session is Friday.
    seed_daily_bars(
        market,
        [("1234", "2026-11-12" if test_case == "previous close" else "2026-11-13", 10.0, 1.0)],
    )
    advance_lake_store_origin(
        market,
        expected=None,
        target=LakeStoreOrigin("other-release", origin.release_manifest_sha256)
        if test_case == "wrong local origin"
        else origin,
    )
    event = tmp_path / "event"
    event.write_text(
        json.dumps(
            {
                "inputs": {
                    "jp_input_probe_ticker": "9999" if test_case == "outside universe" else "1234"
                }
            }
        )
    )
    (tmp_path / "batch-notice.json").write_text(
        json.dumps(
            {
                "asof": meta.as_of.isoformat(),
                "models_ready": test_case != "models failed",
                "daily_run": "2-2" if test_case == "other run" else "2-1",
                "generation_id": "other" if test_case == "other generation" else meta.generation_id,
                "selection_id": identity,
                "lake": {
                    "release_id": origin.release_id,
                    "release_manifest_sha256": origin.release_manifest_sha256,
                },
            }
        )
    )
    for name, value in {
        "GITHUB_EVENT_PATH": str(event),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_REPOSITORY": "x/repo",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "2",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": (
            "x/repo/.github/workflows/cloud-daily-batch.yml@refs/heads/main"
            if test_case == "other workflow"
            else "x/repo/.github/workflows/cloud-daily-batch-current.yml@refs/heads/main"
        ),
    }.items():
        monkeypatch.setenv(name, value)
    before = [sha256(p.read_bytes()).hexdigest() for p in (market, runs)]
    assert main(market=market, runs=runs) == result
    out, err = capsys.readouterr()
    assert stage in out
    assert "no proposal created" in out
    assert err == ""
    assert not any(s in out for s in ("1234", "9999", meta.generation_id, identity))
    assert [sha256(p.read_bytes()).hexdigest() for p in (market, runs)] == before
