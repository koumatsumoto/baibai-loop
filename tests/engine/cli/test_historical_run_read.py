"""保存済み旧runの公開読取CLI。新しい旧runは生成しない。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from tests.helpers.legacy_candidates import _publish, _run

from baibai_engine.cli.historical_evaluation.app import main as screening_main
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import (
    ScreeningRunReader,
)


@pytest.mark.parametrize("command", [["run-show"], ["candidates", "show"]])
def test_public_read_cli_exports_saved_revision_as_json(tmp_path, capsys, command) -> None:
    path = tmp_path / "runs.sqlite"
    raw = _run()
    raw["stock_analyses"][0]["metrics"].update(
        {"label": "0001", "basis": "null", "missing": None, "gain": -0.125, "units": 0}
    )
    _publish(path, raw, revision="run-fixed")
    before = path.read_bytes()
    output = tmp_path / "export.json"
    argv = [
        *command,
        "--run-revision-id",
        "run-fixed",
        "--runs-db",
        str(path),
        "--output-path",
        str(output),
    ]
    assert screening_main(argv) == 0
    emitted = capsys.readouterr().out
    assert emitted == output.read_text()
    payload = json.loads(emitted)
    saved = ScreeningRunReader(path).get_run("run-fixed")
    assert saved is not None
    if command == ["run-show"]:
        assert payload == {
            **saved.payload,
            "run_revision_id": "run-fixed",
            "stock_analyses": list(saved.stock_analyses),
        }
    else:
        assert payload["count"] == 1
        assert payload["candidates"] == list(saved.stock_analyses)
        assert (
            payload["candidate_discovery_method"] == saved.payload["candidate_discovery"]["method"]
        )
    assert "review_set_id" not in payload
    assert path.read_bytes() == before
    assert screening_main(argv) == 1
    assert screening_main([*argv, "--force", "--format", "json"]) == 0
    assert capsys.readouterr().out == emitted


def test_retired_yaml_option_does_not_overwrite_output(tmp_path) -> None:
    output = tmp_path / "saved.txt"
    output.write_text("keep")
    with pytest.raises(SystemExit) as error:
        screening_main(
            [
                "run-show",
                "--run-revision-id",
                "old",
                "--format",
                "yaml",
                "--output-path",
                str(output),
                "--force",
            ]
        )
    assert error.value.code == 2
    assert output.read_text() == "keep"


def test_public_cli_subprocess_reads_zero_candidates_and_unknown_revision(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(rows=[]), revision="empty-run")
    command = [
        sys.executable,
        "-m",
        "baibai_engine.cli.app",
        "historical-evaluation",
        "candidates",
        "show",
        "--runs-db",
        str(path),
        "--format",
        "json",
        "--run-revision-id",
    ]
    completed = subprocess.run([*command, "empty-run"], capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["candidates"] == []
    assert json.loads(completed.stdout)["count"] == 0
    missing = subprocess.run([*command, "unknown"], capture_output=True, text=True, check=False)
    assert missing.returncode == 1
    assert "unknown run_revision_id" in missing.stderr
    assert missing.stdout == ""


def test_removed_review_set_public_cli_is_rejected() -> None:
    with pytest.raises(SystemExit, match="2"):
        screening_main(["review-set", "publish"])


def test_historical_reader_does_not_load_current_candidate_calculation_or_rules():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import ScreeningRunReader; "
                "assert 'baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.candidates' not in sys.modules; "
                "assert 'baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("command", ["run-show", "candidates"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
@pytest.mark.parametrize("path_source", ["argument", "environment", "default"])
def test_forced_export_cannot_replace_input_run_store(
    tmp_path, monkeypatch, capsys, command, alias, path_source
):
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-fixed")
    before = path.read_bytes()
    output = path
    if alias != "direct":
        output = tmp_path / "alias.sqlite"
        if alias == "symlink":
            output.symlink_to(path)
        else:
            output.hardlink_to(path)
    argv = [command]
    if command == "candidates":
        argv.append("show")
    if path_source == "argument":
        argv += ["--runs-db", str(path)]
    elif path_source == "environment":
        monkeypatch.setenv("BAIBAI_RUNS_DB", str(path))
    else:
        import baibai_engine.storage.sqlite.machine_analysis as machine_analysis

        monkeypatch.delenv("BAIBAI_RUNS_DB", raising=False)
        monkeypatch.setattr(machine_analysis, "DEFAULT_RUN_STORE_PATH", path)
    argv += ["--run-revision-id", "run-fixed", "--output-path", str(output), "--force"]
    assert screening_main(argv) == 1
    result = capsys.readouterr()
    assert "must not replace the input run store" in result.err
    assert result.out == ""
    assert path.read_bytes() == before
    assert output.read_bytes() == before
    assert ScreeningRunReader(path).get_run("run-fixed") is not None
