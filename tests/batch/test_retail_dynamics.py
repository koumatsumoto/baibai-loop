from __future__ import annotations

from baibai_batch.jobs import retail_dynamics


def test_manual_job_delegates_explicit_stage_arguments(monkeypatch) -> None:
    calls = []

    def run(argv):
        calls.append(argv)
        return 2

    monkeypatch.setattr(retail_dynamics, "retail_dynamics_main", run)
    args = ["collect", "--output", "/tmp/private-stage.json", "--source", "monex"]
    assert retail_dynamics.main(args) == 2
    assert calls == [args]
