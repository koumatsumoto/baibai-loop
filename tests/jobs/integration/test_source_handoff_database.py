import json
import subprocess
from datetime import date

import pytest

import baibai_jobs.integration.source_handoff as source_handoff
from baibai_jobs.maintenance.market import cli as app


@pytest.mark.parametrize("source", ["jquants-market", "jpx-earnings", "jpx-regulations"])
def test_source_command_preserves_selected_database(source, tmp_path):
    database = tmp_path / "scoped.sqlite"
    command = source_handoff._command(source, date(2026, 10, 24), database=database)
    arguments = app.build_parser().parse_args(command[2:])
    assert arguments.sqlite_path == str(database)


@pytest.mark.parametrize(
    ("source", "replacement"),
    [("edinet-list", "edinet-list-native"), ("jquants-financial", "jquants-financial-native")],
)
def test_retired_source_cannot_build_the_removed_bootstrap_command(tmp_path, source, replacement):
    with pytest.raises(ValueError, match=replacement):
        source_handoff._command(source, date(2026, 11, 7), database=tmp_path / "unused.sqlite")


def test_scoped_acquisition_passes_database_to_child_and_export(tmp_path, mocker, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    database = tmp_path / "scoped.sqlite"
    exported = []

    def export(source, asof, since, selected_database, directory):
        exported.append(selected_database)
        (directory / "result.json").write_text(json.dumps({"source": source}))

    def run(command, **kwargs):
        arguments = app.build_parser().parse_args(command[2:])
        assert arguments.sqlite_path == str(database)
        return subprocess.CompletedProcess(command, 0)

    mocker.patch.object(source_handoff, "_export", side_effect=export)
    mocker.patch.object(source_handoff.subprocess, "run", side_effect=run)
    arguments = [
        "acquire",
        "--source",
        "jpx-earnings",
        "--asof",
        "2026-10-24",
        "--database",
        str(database),
        "--directory",
        str(tmp_path),
    ]
    assert source_handoff.main(arguments) == 0
    assert exported == [database]


@pytest.mark.parametrize("source", ["unknown", "jquants-typo", "edinet-typo", "article-unknown"])
def test_unknown_source_never_reaches_an_existing_owner(source, tmp_path, mocker):
    jquants = mocker.patch.object(source_handoff, "import_jquants_handoff")
    with pytest.raises(ValueError, match="unsupported"):
        source_handoff._replay(source, tmp_path / "market.sqlite", tmp_path)
    jquants.assert_not_called()
