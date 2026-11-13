"""廃止した数値sourceは取得せず、保存済みplan/receiptの読取契約は残す。"""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from tests.helpers.external_assessment_stages import FakeObjectStore, plan

from baibai_jobs.acquisition.external_assessments import cli, collection, daily, sources, staging
from baibai_jobs.acquisition.external_assessments.cohorts import load_plan, plan_bytes


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


@pytest.mark.parametrize(
    ("source", "saved_binding"),
    [
        ("monex", "47eb0e52e995cae364a846f82de9b3e6f87cc49ecf2dcdf3f1e8a50bcecc7c5c"),
        ("kabumap", "b931958e8f39f2afd7c16ec1c5f00ee903714f35da35324cd331a87f9d68d65d"),
    ],
)
def test_surviving_source_binding_does_not_invalidate_existing_cycle(source, saved_binding):
    # Captured from the parent commit, not recomputed using the new source list.
    assert collection.collector_binding(source=source) == saved_binding


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
    selected, _, _ = sources.selection(source="all", batch_id="manual", created_at=None)
    assert [item["source"] for item in selected] == ["monex", "kabumap"]
