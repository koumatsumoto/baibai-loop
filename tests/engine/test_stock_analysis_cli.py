"""保存済み旧runの公開読取CLI。新しい旧runは生成しない。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml
from tests.engine.test_screening_run_store import _publish, _run

from baibai_engine.machine_analysis.screening.cli.app import main as screening_main
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader
from baibai_engine.machine_analysis.screening.screening_run_serializer import published_run_payload


@pytest.mark.parametrize("command", ["run-show", "candidates"])
@pytest.mark.parametrize("output_format", ["yaml", "json"])
def test_public_read_cli_exports_same_revision_without_write(
    tmp_path, capsys, command, output_format
) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-fixed")
    before = path.read_bytes()
    output = tmp_path / "export.txt"
    argv = [command]
    if command == "candidates":
        argv.append("show")
    argv += [
        "--run-revision-id",
        "run-fixed",
        "--runs-db",
        str(path),
        "--format",
        output_format,
        "--output-path",
        str(output),
    ]
    assert screening_main(argv) == 0
    emitted = capsys.readouterr().out
    assert emitted == output.read_text()
    payload = json.loads(emitted) if output_format == "json" else yaml.safe_load(emitted)
    saved = ScreeningRunReader(path).get_run("run-fixed")
    if command == "run-show":
        assert payload == published_run_payload(saved)
    else:
        assert payload["count"] == 1
        assert payload["candidates"] == list(saved.stock_analyses)
        assert (
            payload["candidate_discovery_method"] == saved.payload["candidate_discovery"]["method"]
        )
    assert "review_set_id" not in payload
    assert path.read_bytes() == before
    assert screening_main(argv) == 1
    assert screening_main([*argv, "--force"]) == 0
    assert capsys.readouterr().out == emitted


def test_public_cli_subprocess_reads_zero_candidates_and_unknown_revision(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(rows=[]), revision="empty-run")
    command = [
        sys.executable,
        "-m",
        "baibai_engine.cli",
        "screening",
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
