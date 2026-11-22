"""廃止した数値sourceは取得せず、保存済みplan/receiptの読取契約は残す。"""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml
from tests.helpers.external_assessment_stages import FakeObjectStore, plan

from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    kabumap,
)
from baibai_jobs.acquisition.external_assessments import cli, collection, daily, sources, staging
from baibai_jobs.acquisition.external_assessments.cohorts import load_plan, plan_bytes

# Captured before retirement of Rakuten and Minkabu.
_MONEX_BINDING = "47eb0e52e995cae364a846f82de9b3e6f87cc49ecf2dcdf3f1e8a50bcecc7c5c"
_KABUMAP_V4_BINDING = "b931958e8f39f2afd7c16ec1c5f00ee903714f35da35324cd331a87f9d68d65d"

_KABUMAP_V5_BINDING = "bb0d841f1cb4eaf5b2a9a15b1944835e088aedf67fc59a42f695c4ec15a1f0ed"


@pytest.mark.parametrize("source", ["rakuten", "minkabu"])
def test_retired_source_cannot_start_http_or_initialize_a_store(monkeypatch, tmp_path, source):
    def forbidden(*args, **kwargs):
        pytest.fail("retired acquisition touched HTTP or the store")

    monkeypatch.setattr(collection.requests, "Session", forbidden)
    monkeypatch.setattr(daily, "read_plan", forbidden)
    monkeypatch.setattr(sources, "Boto3R2Store", forbidden)
    with pytest.raises(ValueError, match="supported source"):
        collection.collect(("7203",), sources=(source,))
    with pytest.raises(ValueError, match="supported source"):
        collection.collect_pair("7203", source=source)
    with pytest.raises(ValueError, match="unsupported"):
        collection.collector_binding(source=source)
    with pytest.raises(ValueError, match="unknown source"):
        sources.selection(source=source, batch_id="manual", created_at=None)
    with pytest.raises(ValueError, match="unsupported acquisition source"):
        daily.initialize(
            object(),
            run_id="retired",
            master_asof=date(2026, 11, 12),
            mirror=tmp_path,
            source=source,
        )
    for main, args in (
        (cli.main, ["collect", "--source", source]),
        (sources.main, ["prepare", "--source", source, "--batch-id", "manual"]),
    ):
        with pytest.raises(SystemExit) as error:
            main(args)
        assert error.value.code == 2


@pytest.mark.parametrize("source", ["rakuten", "minkabu"])
def test_retained_plan_is_readable_but_unattempted_pairs_cannot_resume(monkeypatch, source):
    saved = plan(sources=(source,))
    payload = plan_bytes(saved)
    assert plan_bytes(load_plan(payload)) == payload
    selection = [{"source": source, "run_id": f"{source}-historical"}]
    assert sources.read_selection(json.dumps(selection)) == selection
    store = FakeObjectStore()
    staging.save_new_plan(store, saved)
    before = store.objects.copy()
    monkeypatch.setattr(collection.requests, "Session", lambda: pytest.fail("retired HTTP"))
    with pytest.raises(ValueError, match="unsupported external assessment source"):
        daily.acquire_cohort(store, saved, 0)
    assert store.objects == before


def test_retiring_other_sources_preserves_monex_binding():
    assert collection.collector_binding(source="monex") == _MONEX_BINDING


def test_retiring_other_sources_preserves_historical_kabumap_v4_binding(monkeypatch):
    monkeypatch.setattr(kabumap, "PARSER_VERSION", "kabumap-quants-4")
    assert collection.collector_binding(source="kabumap") == _KABUMAP_V4_BINDING


def test_kabumap_v5_binding_does_not_resume_a_pending_v4_plan(monkeypatch):
    assert kabumap.PARSER_VERSION == "kabumap-quants-5"
    current = plan(sources=("kabumap",))
    assert current.collector_binding == _KABUMAP_V5_BINDING

    saved = replace(current, collector_binding=_KABUMAP_V4_BINDING)
    store = FakeObjectStore()
    staging.save_new_plan(store, saved)
    assert staging.read_plan(store, saved.run_id) == saved
    before = store.objects.copy()
    store.events.clear()
    monkeypatch.setattr(sources.requests, "Session", lambda: pytest.fail("v4 plan touched HTTP"))

    with pytest.raises(ValueError, match="collector binding changed"):
        sources.acquire_source(store, saved, source="kabumap")

    assert store.objects == before
    assert all(key != staging.stage_key(saved, 0) for _, key in store.events)


def test_workflow_offers_only_active_sources():
    root = Path(__file__).resolve().parents[4]
    workflow = yaml.load(
        (root / ".github/workflows/cloud-external-assessment.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"]["workflow_dispatch"]["inputs"]["source"]["options"] == [
        "all",
        "monex",
        "kabumap",
    ]
    selected, _ = sources.selection(source="all", batch_id="manual", created_at=None)
    assert [item["source"] for item in selected] == ["monex", "kabumap"]
