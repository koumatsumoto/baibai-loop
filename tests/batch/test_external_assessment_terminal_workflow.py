from __future__ import annotations

import json
from dataclasses import replace

import pytest
from tests.batch.test_external_assessment_publication import publication as publication

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.storage import external_assessment_staging as staging


@pytest.mark.parametrize("old_binding", [False, True])
def test_active_old_multi_source_cli_plan_is_refused_without_changing_handoff(
    publication, monkeypatch, old_binding
):
    store, original, *_ = publication
    p = replace(original, universe_binding=json.dumps({"master_asof": "2026-10-09"}))
    if old_binding:
        p = replace(p, collector_binding="old-binding")
    staging.save_new_plan(store, p)
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    before = {key: value.body for key, value in store.values.items()}
    assert daily.main(["plan", "--run-id", p.run_id, "--master-asof", "2026-10-09"]) == 1
    assert {key: value.body for key, value in store.values.items()} == before
