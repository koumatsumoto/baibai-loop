"""Small runners use bounded local query sessions without changing normal defaults."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from baibai_engine.storage.snapshots import duck


@pytest.mark.parametrize("profile", [None, ""])
def test_default_connection_arguments_and_extension_policy(monkeypatch, profile):
    monkeypatch.delenv("BAIBAI_LAKE_QUERY_PROFILE", raising=False)
    if profile is not None:
        monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", profile)
    calls = []
    statements = []
    closed = []
    connection = SimpleNamespace(execute=statements.append, close=lambda: closed.append(True))

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    monkeypatch.setattr(duck.duckdb, "connect", connect)
    with duck.lake_session() as session:
        assert session.connection is connection
        assert not closed
    assert calls == [((":memory:",), {})]
    assert statements == [
        "SET autoinstall_known_extensions = false",
        "SET autoload_known_extensions = false",
    ]
    assert closed == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_small_runner_isolated_paths_limits_and_cleanup(monkeypatch, tmp_path, fail):
    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", "small-runner")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(duck.tempfile, "tempdir", None)
    calls = []
    closed = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        directory = Path(kwargs["config"]["temp_directory"])
        assert directory.is_dir()
        (directory / "spill").write_text("query spill")

        def close():
            assert directory.is_dir(), "connection must close before spill cleanup"
            closed.append(directory)

        return SimpleNamespace(execute=lambda _: None, close=close)

    monkeypatch.setattr(duck.duckdb, "connect", connect)

    def run():
        with duck.lake_session(), duck.lake_session():
            assert len(calls) == 2
            assert not closed
            if fail:
                raise RuntimeError("query failure")

    if fail:
        with pytest.raises(RuntimeError, match="query failure"):
            run()
    else:
        run()
    paths = []
    for args, kwargs in calls:
        config = kwargs["config"]
        path = Path(config["temp_directory"])
        paths.append(path)
        assert args == (":memory:",)
        assert path.parent == tmp_path
        assert not path.exists()
        assert config == {
            "memory_limit": "1GB",
            "threads": 2,
            "max_temp_directory_size": "4GB",
            "temp_directory": str(path),
        }
    assert paths[0] != paths[1]
    assert closed == list(reversed(paths))


def test_small_runner_actual_connection(monkeypatch):
    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", "small-runner")
    with duck.lake_session() as session:
        settings = dict(
            session.connection.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('threads', 'autoinstall_known_extensions', 'autoload_known_extensions', "
                "'temp_directory')"
            ).fetchall()
        )
        path = Path(settings["temp_directory"])
        assert settings["threads"] == "2"
        assert settings["autoinstall_known_extensions"] == "false"
        assert settings["autoload_known_extensions"] == "false"
        assert path.is_dir()
        assert session.connection.execute("SELECT 42").fetchone() == (42,)
    assert not path.exists()


def test_invalid_profile_rejected_before_connection(monkeypatch):
    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", "invalid")
    monkeypatch.setattr(duck.duckdb, "connect", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(ValueError, match="unsupported lake query profile"), duck.lake_session():
        pass


def test_connection_failure_removes_temporary_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", "small-runner")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(duck.tempfile, "tempdir", None)

    def fail(*args, **kwargs):
        assert Path(kwargs["config"]["temp_directory"]).is_dir()
        raise RuntimeError("connection failed")

    monkeypatch.setattr(duck.duckdb, "connect", fail)
    with pytest.raises(RuntimeError, match="connection failed"), duck.lake_session():
        pass
    assert list(tmp_path.glob("baibai-lake-query-*")) == []
