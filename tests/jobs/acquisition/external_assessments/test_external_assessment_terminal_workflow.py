from __future__ import annotations

import json

import pytest
from tests.helpers.external_assessment_stages import FakeObjectStore, plan, seed_stage

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_jobs.acquisition.external_assessments.cohorts import new_result


def test_history_cli_exposes_read_commands_without_opening_a_store(monkeypatch, capsys):
    monkeypatch.setattr(
        daily, "Boto3R2Store", lambda **kwargs: pytest.fail("argument handling opened R2")
    )
    with pytest.raises(SystemExit) as help_result:
        daily.main(["--help"])
    assert help_result.value.code == 0
    assert "{status,audit,terminal}" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        daily.main(["unknown-command", "--run-id", "external-assessment-daily-2026-10-11"])
    assert error.value.code == 2


def test_saved_history_status_and_audit_are_read_only(monkeypatch, capsys):
    p = plan(sources=("monex", "rakuten"))
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    seed_stage(store, p, new_result(p, 0))
    before = dict(store.objects)
    store.puts.clear()
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    assert daily.main(["status", "--run-id", p.run_id]) == 0
    assert json.loads(capsys.readouterr().out) == {"plan_exists": True}
    assert daily.main(["audit", "--run-id", p.run_id]) == 0
    assert json.loads(capsys.readouterr().out)["state_counts"]["unattempted"] == 2
    assert store.objects == before
    assert store.puts == []
