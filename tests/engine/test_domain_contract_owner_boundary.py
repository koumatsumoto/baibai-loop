"""意味契約の保存形、純粋依存と保存済み読取の境界。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.jpx_total_return import load_benchmark_observation
from baibai_engine.valuation.calibration.query import (
    candidate_er_band_context,
    load_er_calibration_context,
    stored_er_calibration_context,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module",
    [
        "valuation.calibration.contracts",
        "valuation.calibration.query",
        "market_context.observations.benchmark",
    ],
)
def test_contract_and_reader_cold_imports_do_not_load_composition_or_storage(module) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib,json,sys; importlib.import_module(sys.argv[1]); "
                "print(json.dumps(sorted(sys.modules)))"
            ),
            f"baibai_engine.{module}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    forbidden = (
        "baibai_engine.screening",
        "baibai_engine.research",
        "baibai_engine.read_api",
        "baibai_engine.storage",
        "baibai_engine.market",
        "baibai_engine.position",
    )
    assert not [
        name
        for name in json.loads(result.stdout)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]


def test_saved_readers_do_not_write_or_acquire(tmp_path: Path, monkeypatch) -> None:
    import socket

    def reject_network(*_args, **_kwargs):
        pytest.fail("saved contract read attempted network acquisition")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    initial_entries = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    path = tmp_path / "er.yaml"
    path.write_bytes((ROOT / "reports/published/er-level-calibration-latest.yaml").read_bytes())
    before = path.read_bytes()
    raw = safe_load(before)
    assert isinstance(raw, dict)
    common = {
        "expected_rules_hash": raw["screening_rules_hash"],
        "expected_er_model_version": raw["er_model_version"],
        "as_of": date.fromisoformat(raw["valid_through"]),
    }
    loaded = load_er_calibration_context(path, **common)
    assert loaded.artifact is not None
    assert loaded.unavailable_reason is None
    stored, reason = stored_er_calibration_context(path, **common)
    assert reason is None
    assert stored == raw
    # 評価帯境界は上限を含み、観測を更新せず保存済み帯へ対応づける。
    upper = loaded.artifact.horizons[0].bands[0].upper_er_annual
    assert upper is not None
    assert candidate_er_band_context(loaded.artifact, upper)[0]["bands"][0]["band_id"] == "q1"
    assert path.read_bytes() == before
    benchmark_path = tmp_path / "benchmark.yaml"
    benchmark_path.write_bytes(
        (ROOT / "tests/fixtures/benchmark-observation/topix-1y.yaml").read_bytes()
    )
    benchmark_before = benchmark_path.read_bytes()
    assert load_benchmark_observation(benchmark_path).dividend_treatment == "gross_total_return"
    assert benchmark_path.read_bytes() == benchmark_before
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == initial_entries | {
        Path("benchmark.yaml"),
        Path("er.yaml"),
    }


def test_unwritten_er_reader_remains_non_creating_and_stored_reader_raises(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    args = {
        "expected_rules_hash": "unused",
        "expected_er_model_version": "unused",
        "as_of": date(2026, 10, 12),
    }
    assert load_er_calibration_context(path, **args).unavailable_reason == "missing_artifact"
    with pytest.raises(FileNotFoundError):
        stored_er_calibration_context(path, **args)
    assert not path.exists()
