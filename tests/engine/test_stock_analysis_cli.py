"""保存runの公開CLIとDB成功後export失敗からの回収。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime

import pytest
import yaml
from tests.engine.test_screening_cli import (
    FakeEDINETProvider,
    FakeJPXProvider,
    FakeJQuantsProvider,
)
from tests.engine.test_screening_run_store import RULES, _publish, _run

from baibai_engine.screening.cli import main as screening_main
from baibai_engine.screening.cli import run as run_module
from baibai_engine.screening.cli.providers import ProviderBundle
from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.screening.screening_run_serializer import published_run_payload


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


def test_committed_run_export_failure_reports_revision_and_run_show_recovers(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "runs.sqlite"
    output = tmp_path / "failed.yaml"
    config = ScreeningConfig("synthetic-token", "synthetic-key", cache_dir=tmp_path / "cache")
    providers = ProviderBundle(
        jquants=FakeJQuantsProvider(), edinet=FakeEDINETProvider(), jpx=FakeJPXProvider()
    )

    def fail_export(*_args, **_kwargs):
        raise OSError("synthetic file failure")

    monkeypatch.setattr(run_module, "write_text_atomic", fail_export)
    result = run_module.run_command(
        date(2026, 4, 24),
        config,
        providers,
        rules=RULES,
        now=datetime.fromisoformat("2026-04-24T09:00:00+09:00"),
        run_store_path=path,
        run_revision_id="committed-run",
        output_path=output,
    )
    assert result == 1
    diagnostics = capsys.readouterr().err
    assert "run_revision_id=committed-run" in diagnostics
    assert "export failed" in diagnostics
    saved = ScreeningRunReader(path).get_run("committed-run")
    assert saved is not None
    assert not output.exists()
    recovered = tmp_path / "recovered.yaml"
    assert (
        screening_main(
            [
                "run-show",
                "--run-revision-id",
                "committed-run",
                "--runs-db",
                str(path),
                "--output-path",
                str(recovered),
            ]
        )
        == 0
    )
    assert yaml.safe_load(recovered.read_text()) == published_run_payload(saved)
    assert len(ScreeningRunReader(path).list_runs()) == 1
