"""Regression tests for the autouse database isolation contract in conftest."""

from __future__ import annotations

import os
from pathlib import Path

from baibai_engine.appdb.write import DEFAULT_DB_PATH, database_path
from baibai_engine.screening.run_store import run_store_path
from baibai_engine.screening.run_store.store import DEFAULT_RUN_STORE_PATH


def test_default_db_resolution_is_redirected_away_from_operational_stores() -> None:
    assert os.environ["BAIBAI_DB"]
    assert os.environ["BAIBAI_RUNS_DB"]
    assert database_path() != DEFAULT_DB_PATH
    assert run_store_path() != DEFAULT_RUN_STORE_PATH
    assert Path(os.environ["BAIBAI_DB"]) != DEFAULT_DB_PATH.resolve()
    assert Path(os.environ["BAIBAI_RUNS_DB"]) != DEFAULT_RUN_STORE_PATH.resolve()


def test_each_test_gets_a_writable_isolated_db_location() -> None:
    resolved = database_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(b"isolated")
    assert resolved.read_bytes() == b"isolated"
    assert resolved.resolve() != DEFAULT_DB_PATH.resolve()
