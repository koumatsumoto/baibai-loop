from pathlib import Path

import yaml
from tests.helpers.workflow_schedule import declared_schedule

ROOT = Path(__file__).resolve().parents[3]


def test_article_slots_use_bounded_host_groups_without_model_generation():
    raw = (ROOT / ".github/workflows/cloud-article-sources.yml").read_text()
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert declared_schedule(raw) == [{"cron": "13 1,5,9,13,17,21 * * *"}]
    job = workflow["jobs"]["acquire"]
    assert job["strategy"]["matrix"]["group"] == ["public", "world", "rakuten"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["group"]["options"] == [
        "all",
        "public",
        "world",
        "rakuten",
    ]
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


def test_manual_and_scheduled_article_dispatch_share_one_acquisition_contract():
    workflow = yaml.load(
        (ROOT / ".github/workflows/cloud-article-sources.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["acquire"]["steps"]
    acquire = next(step for step in steps if step.get("name", "").startswith("Acquire due"))
    assert "--manual" not in acquire["run"]
    assert '--created-at "$CREATED_AT"' in acquire["run"]
