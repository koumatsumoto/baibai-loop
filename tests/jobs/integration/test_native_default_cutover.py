"""Migrated production callers use native L1 even when old repository vars remain false."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.calendar_cutover import calendar_native_enabled
from baibai_jobs.financial_cutover import financial_native_enabled
from baibai_jobs.master_cutover import master_native_enabled
from baibai_jobs.price_cutover import price_native_enabled

MODES = {
    "FINANCIAL": financial_native_enabled,
    "PRICE": price_native_enabled,
    "MASTER": master_native_enabled,
    "CALENDAR": calendar_native_enabled,
}


@pytest.mark.parametrize("source", MODES)
def test_unconfigured_runtime_defaults_to_native_after_finite_cutover(monkeypatch, source):
    monkeypatch.delenv(f"BAIBAI_{source}_NATIVE", raising=False)
    assert MODES[source]() is True


@pytest.mark.parametrize("source", MODES)
def test_explicit_legacy_mode_remains_available_for_fixed_old_inputs(monkeypatch, source):
    monkeypatch.setenv(f"BAIBAI_{source}_NATIVE", "false")
    assert MODES[source]() is False
    monkeypatch.setenv(f"BAIBAI_{source}_NATIVE", "invalid")
    with pytest.raises(ValueError, match="true or false"):
        MODES[source]()


@pytest.mark.parametrize(
    ("filename", "job", "sources"),
    [
        ("cloud-daily-batch.yml", None, set(MODES)),
        ("cloud-market-source.yml", None, set(MODES)),
        ("cloud-tradingview-snapshot.yml", None, {"MASTER", "CALENDAR"}),
        ("cloud-external-assessment-source.yml", "acquire", {"MASTER", "CALENDAR"}),
    ],
)
def test_all_explicit_workflow_callers_ignore_old_false_repository_vars(
    monkeypatch, filename, job, sources
):
    workflow = yaml.load((Path(".github/workflows") / filename).read_text(), Loader=yaml.BaseLoader)
    env = (workflow if job is None else workflow["jobs"][job])["env"]
    for source in sources:
        key = f"BAIBAI_{source}_NATIVE"
        monkeypatch.setenv(key, "false")
        assert env[key] == "true"  # No vars expression can reinstate the old writer.
        monkeypatch.setenv(key, env[key])
        assert MODES[source]() is True
    if filename in {"cloud-daily-batch.yml", "cloud-tradingview-snapshot.yml"}:
        assert "schedule" not in workflow["on"]


def test_no_jquants_native_repository_var_reader_is_left_in_workflows():
    for path in Path(".github/workflows").glob("*.yml"):
        text = path.read_text()
        for source in MODES:
            assert f"vars.BAIBAI_{source}_NATIVE" not in text
