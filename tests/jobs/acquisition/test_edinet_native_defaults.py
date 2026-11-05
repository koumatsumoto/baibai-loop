"""Migrated EDINET callers consistently use native originals by default."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.edinet_cutover import edinet_list_native_enabled


def test_unconfigured_runtime_defaults_to_native(monkeypatch):
    monkeypatch.delenv("BAIBAI_EDINET_LIST_NATIVE", raising=False)
    assert edinet_list_native_enabled() is True


@pytest.mark.parametrize("value", ["true", "false", "TRUE", "1", "yes", " true"])
def test_explicit_mode_remains_strict(monkeypatch, value):
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", value)
    if value in {"true", "false"}:
        assert edinet_list_native_enabled() is (value == "true")
    else:
        with pytest.raises(ValueError, match="true or false"):
            edinet_list_native_enabled()


@pytest.mark.parametrize("filename", ["cloud-daily-batch.yml", "cloud-market-source.yml"])
def test_acquisition_and_daily_callers_ignore_old_false_repository_vars(filename, monkeypatch):
    workflow = yaml.load((Path(".github/workflows") / filename).read_text(), Loader=yaml.BaseLoader)
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "false")
    assert workflow["env"]["BAIBAI_EDINET_LIST_NATIVE"] == "true"
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", workflow["env"]["BAIBAI_EDINET_LIST_NATIVE"])
    assert edinet_list_native_enabled() is True
    if filename == "cloud-daily-batch.yml":
        assert "schedule" not in workflow["on"]
    else:
        steps = {step.get("name"): step for step in workflow["jobs"]["acquire"]["steps"]}
        for name in (
            "Load only the assigned source input",
            "Read own confirmed originals and required EDINET list receipts",
        ):
            assert steps[name]["if"] == (
                '${{ !contains(fromJSON(\'["jquants-market","jquants-financial",'
                '"edinet-list"]\'), inputs.source) }}'
            )


def test_no_edinet_native_repository_var_reader_is_left_in_workflows():
    for path in Path(".github/workflows").glob("*.yml"):
        assert "vars.BAIBAI_EDINET_LIST_NATIVE" not in path.read_text()
