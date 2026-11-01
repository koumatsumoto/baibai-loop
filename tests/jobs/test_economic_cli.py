"""Read-only economic CLI entrypoints must not acquire or open stores."""

import pytest


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

