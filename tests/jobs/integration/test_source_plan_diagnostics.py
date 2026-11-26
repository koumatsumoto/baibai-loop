"""計画段階の経過ログは固定入力・発見順・失敗境界へ入らない。"""

import json
from types import SimpleNamespace

import pytest

from baibai_jobs.integration import sources

PHASES = ("external_ready", "market_handoffs", "edinet_input")


def setup_plan(monkeypatch, *, fail=None):
    calls = []
    values = {
        "external_ready": ([{"run_id": "private-ready"}], [{"reason": "ValueError"}]),
        "market_handoffs": [
            {"source": "jquants-market"},
            {"source": "jquants-financial"},
            {"source": "jpx-earnings"},
        ],
        "edinet_input": SimpleNamespace(model_dump=lambda **_: {"sha256": "private-fixed"}),
    }

    def phase(name):
        def run(*args, **kwargs):
            calls.append(name)
            if name == fail:
                raise ValueError("private failure detail")
            if name == "market_handoffs":
                assert kwargs == {"market": True, "exclude_equity_tradingview": True}
            return values[name]

        return run

    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: SimpleNamespace(close=lambda: None))
    for name, target in zip(
        PHASES,
        ("available_ready", "available_handoffs", "fix_edinet_input"),
        strict=True,
    ):
        monkeypatch.setattr(sources, target, phase(name))
    clock = iter([10.0, 11.25, 20.0, 22.5, 40.0, 45.0])
    monkeypatch.setattr(sources.time, "perf_counter", lambda: next(clock))
    return calls


def test_plan_timings_leave_fixed_values_and_call_order_unchanged(tmp_path, monkeypatch, capsys):
    calls = setup_plan(monkeypatch)
    assert sources.main(["plan", "--directory", str(tmp_path)]) == 0
    assert calls == list(PHASES)
    assert json.loads((tmp_path / "plan.json").read_text()) == {
        "handoffs": [{"source": "jpx-earnings"}],
        "edinet_input": {"sha256": "private-fixed"},
        "external": [{"run_id": "private-ready"}],
        "external_failed": [{"reason": "ValueError"}],
    }
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.splitlines() == [
        line
        for name, seconds in zip(PHASES, (1.25, 2.5, 5.0), strict=True)
        for line in (
            f"source plan: phase={name} state=start",
            f"source plan: phase={name} state=done elapsed_seconds={seconds:.3f}",
        )
    ]


@pytest.mark.parametrize("failed_phase", PHASES)
def test_failed_phase_stops_without_plan_later_phase_or_private_log(
    tmp_path, monkeypatch, capsys, failed_phase
):
    calls = setup_plan(monkeypatch, fail=failed_phase)
    assert sources.main(["plan", "--directory", str(tmp_path)]) == 1
    assert calls == list(PHASES[: PHASES.index(failed_phase) + 1])
    assert not (tmp_path / "plan.json").exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    assert f"phase={failed_phase} state=done" not in output.err
    assert output.err.endswith("common source integration failed: ValueError\n")


def test_existing_plan_is_not_retimed_or_rewritten(tmp_path, monkeypatch, capsys):
    calls = setup_plan(monkeypatch)
    path = tmp_path / "plan.json"
    path.write_bytes(b'{"fixed":"unchanged"}\n')
    assert sources.main(["plan", "--directory", str(tmp_path)]) == 1
    assert calls == []
    assert path.read_bytes() == b'{"fixed":"unchanged"}\n'
    assert capsys.readouterr().err == "common source integration failed: FileExistsError\n"
