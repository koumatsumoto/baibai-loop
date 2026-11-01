"""Collection CLI listing stays read-only; refresh reports each source outcome."""

import unittest

import pytest
from tests.helpers.network import _no_real_network as _no_real_network

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_jobs.acquisition.economic_cli import main as economic_main


def test_collection_list_and_help_do_not_load_env_or_create_a_store(tmp_path, monkeypatch, capsys):
    import json
    import sqlite3

    import baibai_jobs.acquisition.economic_cli as economic_cli
    from baibai_jobs import cli

    def unexpected(*args, **kwargs):
        raise AssertionError("read-only Collection command opened a store or loaded env")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    monkeypatch.setattr(economic_cli, "load_project_env", unexpected)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    assert cli.main(["acquire-economic", "list", "--format", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "fed-h15" in {item["source_id"] for item in listed}
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["acquire-economic", "--help"])
    assert help_exit.value.code == 0
    assert "refresh" in capsys.readouterr().out
    assert set(tmp_path.rglob("*")) == before


class EconomicRefreshCliTests(unittest.TestCase):
    def test_refresh_cli_requires_one_range_mode(self) -> None:
        with self.assertRaises(SystemExit):
            economic_main(
                [
                    "refresh",
                    "fed-h15",
                    "--start",
                    "2026-01-01",
                    "--all-history",
                    "--end",
                    "2026-07-20",
                ]
            )
        with self.assertRaises(SystemExit):
            economic_main(["refresh", "fed-h15", "--end", "2026-07-20"])


def test_native_refresh_cli_reports_each_source_failure(tmp_path, monkeypatch, capsys):
    from baibai_engine.data_collection.transport.context import ProviderFetchStoppedError

    def fail(source, **kwargs):
        raise ProviderFetchStoppedError(f"{source.source_id} unavailable")

    monkeypatch.setattr(acquisition, "fetch_native_records", fail)
    status = economic_main(
        [
            "refresh",
            "mof-jgbcm-all",
            "fed-h15",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-20",
            "--db",
            str(tmp_path / "indicators.sqlite"),
        ]
    )
    assert status == 1
    report = capsys.readouterr().err
    assert "- mof-jgbcm-all: mof-jgbcm-all unavailable" in report
    assert "- fed-h15: fed-h15 unavailable" in report
    assert report.strip().splitlines()[-1] == (
        "error: refresh failed for 2 of 2 sources: mof-jgbcm-all, fed-h15"
    )
