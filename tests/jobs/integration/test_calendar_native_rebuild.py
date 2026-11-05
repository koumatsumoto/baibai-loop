"""Saved native calendar reconstruction is independent of heads, flags and providers."""

import json
from datetime import timedelta

import pyarrow.parquet as pq
import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY, attempt
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import HEAD_KEY, CalendarL1
from baibai_jobs.integration import calendar_native as integration


def saved_calendar(tmp_path):
    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    source = CalendarL1(store, tmp_path / "native")
    source.initialize()
    source.commit(
        [
            attempt("old", DAY.replace(year=DAY.year - 1)),
            attempt("today"),
            attempt(
                "unknown",
                DAY + timedelta(days=1),
                rows=[{"Date": (DAY + timedelta(days=1)).isoformat(), "HolDiv": "UNKNOWN"}],
            ),
            attempt("partial", DAY + timedelta(days=2), partial=True),
        ]
    )
    saved = integration.fixed_input(store, source.fixed(), as_of=DAY, evaluated_at=AT)
    path = tmp_path / "saved.json"
    path.write_text(saved.model_dump_json())
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    return store, path, saved


def test_rebuild_cli_cold_saved_input_outputs_entire_selected_history(
    tmp_path, monkeypatch, capsys
):
    store, path, saved = saved_calendar(tmp_path)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "not-even-a-valid-switch")
    before = dict(store.values)
    output = tmp_path / "cold"
    assert integration.main(["--saved-input", str(path), "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 2
    assert summary["failed_days"] == summary["unknown_division_days"] == 1
    assert pq.read_table(output / "calendar.parquet").to_pylist() == [
        {"day": DAY.replace(year=DAY.year - 1).isoformat(), "holiday_division": "1"},
        {"day": DAY.isoformat(), "holiday_division": "1"},
    ]
    coverage = pq.read_table(output / "coverage.parquet").to_pylist()
    assert len(coverage) == (DAY + timedelta(days=2) - DAY.replace(year=DAY.year - 1)).days + 1
    assert sum(item["status"] == "ok" for item in coverage) == 2
    assert all(item["record_count"] == 0 for item in coverage if item["status"] != "ok")
    assert json.loads((output / "input.json").read_bytes())["saved_input"] == saved.model_dump(
        mode="json"
    )
    assert {item.name for item in output.iterdir()} == {
        "calendar.parquet",
        "coverage.parquet",
        "input.json",
    }
    assert HEAD_KEY not in store.get_keys
    assert store.values == before
    assert integration.main(["--saved-input", str(path), "--output", str(output)]) == 1
    assert "FileExistsError" in capsys.readouterr().err


def test_rebuild_parser_requires_saved_input_and_has_no_live_fallback(capsys):
    with pytest.raises(SystemExit) as exit:
        integration.main(["--help"])
    assert exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--saved-input" in help_text
    assert "--output" in help_text
    assert "--work-directory" not in help_text
    with pytest.raises(SystemExit) as exit:
        integration.main(["--output", "anything"])
    assert exit.value.code == 2


def test_jobs_dispatch_rebuilds_saved_calendar_without_head(tmp_path, monkeypatch):
    from baibai_jobs import cli

    store, path, _ = saved_calendar(tmp_path)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    output = tmp_path / "dispatched"
    assert (
        cli.main(["rebuild-calendar-native", "--saved-input", str(path), "--output", str(output)])
        == 0
    )
    assert len(pq.read_table(output / "calendar.parquet")) == 2
    assert HEAD_KEY not in store.get_keys
