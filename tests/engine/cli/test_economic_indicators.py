"""Indicator CLI formatting and argument failures; reads never acquire inputs."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.indicator_store import (
    _store_multpl_earnings_yield,
    observation,
    store_h15_observations,
)
from tests.helpers.network import _no_real_network as _no_real_network

from baibai_engine.cli.economic_indicators import main
from baibai_engine.data_collection.receipts.native_sources.store import open_connection


class IndicatorCliTests(unittest.TestCase):
    def test_cli_handles_bad_db_path_without_traceback(self) -> None:
        self.assertEqual(main(["get", "us.10y", "--latest", "--db", "/tmp"]), 1)

    def test_cli_unknown_series_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "indicators.sqlite"

            self.assertEqual(main(["get", "jp.cpi.stale", "--latest", "--db", str(db)]), 1)


def test_derived_cli_reports_computation_without_source_fetch(tmp_path, capsys):
    database = tmp_path / "indicators.sqlite"
    _store_multpl_earnings_yield(database, date(2026, 1, 1), "5.0")
    with closing(open_connection(database)) as connection:
        store_h15_observations(
            connection,
            [observation("us.10y", date(2026, 1, 30), 4.0, datetime(2026, 2, 1, tzinfo=UTC))],
        )
        connection.commit()
    with (
        patch(
            "baibai_engine.data_collection.acquisition.refresh.fetch_native_records",
            side_effect=AssertionError("derived CLI must not acquire native data"),
        ),
    ):
        code = main(
            [
                "get",
                "us.erp",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--db",
                str(database),
            ]
        )
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "series_id\tobserved_at\tvalue\tunit\tprovider",
        "us.erp\t2026-01-30\t1\tpercent\tderived",
    ]


def test_catalog_never_opens_a_store(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("catalog opened a store")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    before = set(tmp_path.rglob("*"))
    monkeypatch.chdir(tmp_path)
    for command in (["list", "--format", "json"], ["search", "CPI"]):
        assert main(command) == 0
    assert set(tmp_path.rglob("*")) == before
    with pytest.raises(SystemExit):
        main(["list", "--db", str(tmp_path / "missing.sqlite")])
