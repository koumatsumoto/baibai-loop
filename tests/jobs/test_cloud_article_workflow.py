from pathlib import Path

import yaml

from baibai_jobs.acquisition.article_planning import ARTICLE_CRON

ROOT = Path(__file__).resolve().parents[2]


def test_article_slots_use_bounded_host_groups_without_model_generation():
    raw = (ROOT / ".github/workflows/cloud-article-sources.yml").read_text()
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": ARTICLE_CRON}]
    job = workflow["jobs"]["acquire"]
    assert job["strategy"]["matrix"]["group"] == ["public", "minkabu", "rakuten"]
    assert job["strategy"]["fail-fast"] == "false"
    assert "source-host-" in job["concurrency"]["group"]
    assert "model-integration" not in raw
    assert "upload-artifact" not in raw
    collector = yaml.load(
        (ROOT / ".github/workflows/cloud-external-assessment-source.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "source-host-" in collector["jobs"]["acquire"]["concurrency"]["group"]
    assert "minkabu" in collector["jobs"]["acquire"]["concurrency"]["group"]
    assert "rakuten" in collector["jobs"]["acquire"]["concurrency"]["group"]


def test_manual_article_dispatch_passes_manual_flag_without_changing_clock():
    workflow = yaml.load(
        (ROOT / ".github/workflows/cloud-article-sources.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["acquire"]["steps"]
    acquire = next(step for step in steps if step.get("name", "").startswith("Acquire due"))
    assert (
        'if [ "$GITHUB_EVENT_NAME" = workflow_dispatch ]; then args+=(--manual); fi'
        in acquire["run"]
    )
    assert '--created-at "$CREATED_AT"' in acquire["run"]
