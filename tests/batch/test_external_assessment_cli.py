from __future__ import annotations

from baibai_batch.jobs import external_assessment as external_assessment_dynamics


def test_manual_job_delegates_explicit_stage_arguments(monkeypatch) -> None:
    calls = []

    def run(argv):
        calls.append(argv)
        return 2

    monkeypatch.setattr(external_assessment_dynamics, "external_assessment_acquisition_main", run)
    args = ["collect", "--output", "/tmp/private-stage.json", "--source", "monex"]
    assert external_assessment_dynamics.main(args) == 2
    assert calls == [args]
