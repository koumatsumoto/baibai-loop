import json
import subprocess
from datetime import date

import pytest

from baibai_engine.source_maintenance import cli as app
from baibai_jobs.jobs import source_handoff


@pytest.mark.parametrize("source", source_handoff.SOURCES[1:])
def test_source_command_preserves_selected_database(source, tmp_path):
    database = tmp_path / "scoped.sqlite"
    command = source_handoff._command(source, date(2026, 10, 24), database=database)
    arguments = app.build_parser().parse_args(command[3:])
    assert arguments.sqlite_path == str(database)


@pytest.mark.parametrize("source", ["edinet-metrics", "edinet-facts"])
def test_replay_extracts_from_the_same_database_as_import(source, tmp_path, mocker):
    database = tmp_path / "scoped.sqlite"
    (tmp_path / "result.json").write_text(json.dumps({"source": source, "asof": "2026-10-24"}))
    imported = mocker.patch.object(source_handoff, "import_edinet_handoff", return_value={})
    reader = mocker.patch.object(app, "EDINETReader")
    handler = mocker.patch.object(
        app,
        "extract_edinet_metrics_command"
        if source == "edinet-metrics"
        else "extract_edinet_facts_command",
        return_value=0,
    )

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, app.main(command[3:]), "", "")

    mocker.patch.object(source_handoff.subprocess, "run", side_effect=run)
    result = source_handoff._replay(source, database, tmp_path)
    imported.assert_called_once_with(tmp_path / "receipts.sqlite", database)
    reader.assert_called_once_with(database)
    assert handler.call_args.kwargs["sqlite_path"] == database
    assert result["extraction_exit_code"] == 0


def test_scoped_acquisition_passes_database_to_child_and_export(tmp_path, mocker, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    database = tmp_path / "scoped.sqlite"
    exported = []

    def export(source, asof, since, selected_database, directory, archives):
        exported.append(selected_database)
        (directory / "result.json").write_text(json.dumps({"source": source}))

    def run(command, **kwargs):
        arguments = app.build_parser().parse_args(command[3:])
        assert arguments.sqlite_path == str(database)
        return subprocess.CompletedProcess(command, 0)

    mocker.patch.object(source_handoff, "_export", side_effect=export)
    mocker.patch.object(source_handoff.subprocess, "run", side_effect=run)
    arguments = [
        "acquire",
        "--source",
        "jquants-market",
        "--asof",
        "2026-10-24",
        "--database",
        str(database),
        "--directory",
        str(tmp_path),
    ]
    assert source_handoff.main(arguments) == 0
    assert exported == [database]
