"""Merging existing stores must not recreate a target lost after its precheck."""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_jobs.integration import native_source_merge

_CASES = [
    pytest.param(native_source_merge.merge_stores, initialize_database, id="economic-union"),
    pytest.param(
        native_source_merge.reconcile_stores, initialize_database, id="economic-reconcile"
    ),
]


@pytest.mark.parametrize(("merge", "initialize"), _CASES)
def test_merge_does_not_recreate_a_target_lost_after_precheck(
    tmp_path, monkeypatch, merge, initialize
):
    source, target = tmp_path / "source.sqlite", tmp_path / "target ?#.sqlite"
    retained = tmp_path / "retained.sqlite"
    for path in (source, target):
        with closing(initialize(path)):
            pass
    before_source, before_target = source.read_bytes(), target.read_bytes()
    before_paths = set(tmp_path.iterdir())
    uri = target.resolve().as_uri()
    real_connect = sqlite3.connect
    write_attempts = []

    def connect(database, *args, **kwargs):
        if str(database).split("?")[0] == uri and kwargs.get("isolation_level", "default") is None:
            write_attempts.append(str(database))
            target.rename(retained)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(MergeError, match="could not open existing merge target"):
        merge(source, target)
    assert write_attempts == [uri + "?mode=rw"]
    assert not target.exists()
    assert source.read_bytes() == before_source
    assert retained.read_bytes() == before_target
    assert set(tmp_path.iterdir()) == before_paths - {target} | {retained}


@pytest.mark.parametrize(("merge", "initialize"), _CASES)
def test_merge_does_not_initialize_an_empty_target(tmp_path, merge, initialize):
    source, target = tmp_path / "source.sqlite", tmp_path / "empty.sqlite"
    with closing(initialize(source)):
        pass
    target.touch()
    before_paths = set(tmp_path.iterdir())
    before_source = source.read_bytes()
    with pytest.raises(MergeError, match="schema"):
        merge(source, target)
    assert target.read_bytes() == b""
    assert source.read_bytes() == before_source
    assert set(tmp_path.iterdir()) == before_paths


def test_cli_reports_target_open_failure_without_success_output(tmp_path, monkeypatch, capsys):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    for path in (source, target):
        with closing(initialize_database(path)):
            pass
    before = {path: path.read_bytes() for path in (source, target)}
    uri = target.resolve().as_uri()
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        if str(database).split("?")[0] == uri and kwargs.get("isolation_level", "default") is None:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert native_source_merge.main(["--canonical", str(source), "--target", str(target)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "could not open existing merge target" in output.err
    assert str(target) in output.err
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(("merge", "initialize"), _CASES)
def test_merge_accepts_an_existing_target_with_uri_characters(tmp_path, merge, initialize):
    source, target = tmp_path / "source.sqlite", tmp_path / "target ?#.sqlite"
    for path in (source, target):
        with closing(initialize(path)):
            pass
    before_source = source.read_bytes()
    assert merge(source, target).inserted == 0
    assert merge(source, target).inserted == 0
    assert source.read_bytes() == before_source
    assert target.is_file()
