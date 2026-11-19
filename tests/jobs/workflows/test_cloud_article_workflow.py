import subprocess
from pathlib import Path

import pytest
import yaml
from tests.helpers.workflow_schedule import declared_schedule

ROOT = Path(__file__).resolve().parents[3]


def test_article_slots_use_bounded_host_groups_without_model_generation():
    raw = (ROOT / ".github/workflows/cloud-article-sources.yml").read_text()
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert declared_schedule(raw) == [{"cron": "13 1,5,9,13,17,21 * * *"}]
    job = workflow["jobs"]["acquire"]
    assert job["strategy"]["matrix"]["group"] == (
        "${{ fromJSON(github.event_name == 'schedule' && "
        '\'["scheduled"]\' || \'["public","world","rakuten"]\') }}'
    )
    assert workflow["on"]["workflow_dispatch"]["inputs"]["group"]["options"] == [
        "all",
        "public",
        "world",
        "rakuten",
    ]
    assert job["strategy"]["fail-fast"] == "false"
    assert job["concurrency"]["group"] == (
        "source-host-${{ (matrix.group == 'public' || matrix.group == 'scheduled') "
        "&& 'article-public' || matrix.group }}"
    )
    assert job["concurrency"]["queue"] == "max"
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert "model-integration" not in raw
    assert "upload-artifact" not in raw
    collector = yaml.load(
        (ROOT / ".github/workflows/cloud-external-assessment-source.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "source-host-" in collector["jobs"]["acquire"]["concurrency"]["group"]
    assert collector["jobs"]["acquire"]["concurrency"]["group"] == (
        "source-host-external-${{ inputs.source }}"
    )


def test_manual_and_scheduled_article_dispatch_share_one_acquisition_contract():
    workflow = yaml.load(
        (ROOT / ".github/workflows/cloud-article-sources.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["acquire"]["steps"]
    acquire = next(step for step in steps if step.get("name", "").startswith("Acquire due"))
    assert acquire["env"]["GROUP"] == "${{ matrix.group }}"
    assert "--manual" not in acquire["run"]
    assert '--created-at "$CREATED_AT"' in acquire["run"]


@pytest.mark.parametrize(
    ("requested", "group", "selected"),
    [
        ("all", "scheduled", True),
        ("all", "public", True),
        ("public", "public", True),
        ("public", "world", False),
        ("rakuten", "rakuten", True),
    ],
)
def test_article_scope_step_selects_scheduled_and_manual_groups(
    tmp_path, requested, group, selected
):
    workflow = yaml.load(
        (ROOT / ".github/workflows/cloud-article-sources.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["acquire"]["steps"]
    scope = next(step for step in steps if step.get("id") == "scope")
    acquire = next(step for step in steps if step.get("name", "").startswith("Acquire due"))
    assert scope["env"]["REQUESTED_GROUP"] == "${{ inputs.group || 'all' }}"
    assert acquire["if"] == "steps.scope.outputs.selected == 'true'"
    output = tmp_path / "output"
    output.touch()
    subprocess.run(
        ["bash", "-eu", "-c", scope["run"]],
        check=True,
        env={"REQUESTED_GROUP": requested, "GROUP": group, "GITHUB_OUTPUT": str(output)},
    )
    assert output.read_text() == ("selected=true\n" if selected else "")
