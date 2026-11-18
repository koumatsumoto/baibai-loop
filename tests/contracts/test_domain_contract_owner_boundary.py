"""意味契約の保存形、純粋依存と保存済み読取の境界。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.observations.prices.jpx_total_return import (
    load_benchmark_observation,
)

ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_cold_import_does_not_load_composition_or_storage() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib,json,sys; importlib.import_module(sys.argv[1]); "
                "print(json.dumps(sorted(sys.modules)))"
            ),
            "baibai_engine.machine_analysis.observations.prices.benchmark_observation",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    forbidden = (
        "baibai_engine.machine_analysis.screening",
        "baibai_engine.investment_judgment",
        "baibai_engine.storage",
        "baibai_engine.portfolio",
    )
    assert not [
        name
        for name in json.loads(result.stdout)
        if name != "baibai_engine.storage"
        and any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]


def test_saved_benchmark_does_not_write_or_acquire(tmp_path: Path, monkeypatch) -> None:
    import socket

    def reject_network(*_args, **_kwargs):
        pytest.fail("saved contract read attempted network acquisition")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    initial_entries = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    benchmark_path = tmp_path / "benchmark.yaml"
    benchmark_path.write_bytes(
        (ROOT / "tests/fixtures/benchmark-observation/topix-1y.yaml").read_bytes()
    )
    benchmark_before = benchmark_path.read_bytes()
    assert load_benchmark_observation(benchmark_path).dividend_treatment == "gross_total_return"
    assert benchmark_path.read_bytes() == benchmark_before
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == initial_entries | {
        Path("benchmark.yaml"),
    }
