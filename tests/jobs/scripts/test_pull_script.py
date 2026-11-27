from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _isolated_pull_script(tmp_path: Path) -> tuple[Path, Path]:
    scripts = tmp_path / "jobs" / "scripts"
    scripts.mkdir(parents=True)
    pull = scripts / "pull.sh"
    shutil.copy2(Path(__file__).resolve().parents[3] / "jobs/scripts/pull.sh", pull)
    transfer = scripts / "r2_transfer.sh"
    transfer.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$1" >> "$PULL_CALL_LOG"\n'
        'if [[ "$1" == "$PULL_FAIL_STAGE" ]]; then exit 7; fi\n',
        encoding="utf-8",
    )
    transfer.chmod(0o755)
    calls = tmp_path / "calls.log"
    return pull, calls


def test_pull_prepares_market_before_reporting_success(tmp_path: Path) -> None:
    pull, calls = _isolated_pull_script(tmp_path)
    for failure, expected in (
        ("", ["pull-machine", "pull-indicators"]),
        ("pull-machine", ["pull-machine"]),
        ("pull-indicators", ["pull-machine", "pull-indicators"]),
    ):
        calls.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(pull)],
            env={**os.environ, "PULL_CALL_LOG": str(calls), "PULL_FAIL_STAGE": failure},
            capture_output=True,
            text=True,
            check=False,
        )
        assert calls.read_text().splitlines() == expected
        assert result.returncode == (7 if failure else 0)
        assert ("market hydrated" in result.stdout) is (not failure)
        if failure == "pull-indicators":
            assert "Market/Runs are already updated" in result.stderr
            assert "jobs/scripts/r2_transfer.sh pull-indicators" in result.stderr
        else:
            assert result.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("-h",), 0),
        (("--help",), 0),
        (("unexpected",), 2),
        (("--dry-run",), 2),
        (("--help", "unexpected"), 2),
    ],
)
def test_pull_arguments_stop_before_transfer(
    tmp_path: Path, arguments: tuple[str, ...], code: int
) -> None:
    pull, calls = _isolated_pull_script(tmp_path)
    result = subprocess.run(
        [str(pull), *arguments],
        cwd=tmp_path,
        env={**os.environ, "PULL_CALL_LOG": str(calls), "PULL_FAIL_STAGE": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code
    output = result.stderr if code else result.stdout
    assert output.startswith("Usage: jobs/scripts/pull.sh\n")
    assert not (result.stdout if code else result.stderr)
    assert not calls.exists()
