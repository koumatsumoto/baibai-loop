from __future__ import annotations

import json

import pytest
from tests.jobs.test_external_assessment_daily import FakeObjectStore, plan, seed_stage

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_jobs.acquisition.external_assessments.cohorts import new_result


@pytest.mark.parametrize("command", ["plan", "acquire", "publish", "publish-confirmed-partial"])
def test_legacy_cli_has_no_write_entry_even_for_saved_plans(monkeypatch, command):
    monkeypatch.setattr(
        daily, "Boto3R2Store", lambda **kwargs: pytest.fail("write entry opened R2")
    )
    with pytest.raises(SystemExit) as error:
        daily.main([command, "--run-id", "external-assessment-daily-2026-10-11"])
    assert error.value.code == 2


def test_legacy_status_and_audit_are_read_only(monkeypatch, capsys):
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
