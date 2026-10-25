from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_SCRIPT = ROOT / "jobs" / "scripts" / "publish.sh"


def _isolated_publish_script(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    scripts = root / "jobs" / "scripts"
    scripts.mkdir(parents=True)
    publish = scripts / "publish.sh"
    shutil.copy2(PUBLISH_SCRIPT, publish)

    call_log = tmp_path / "calls.log"
    transfer = scripts / "r2_transfer.sh"
    transfer.write_text(
        '#!/usr/bin/env bash\nprintf \'r2:%s\\n\' "$*" >> "${PUBLISH_CALL_LOG}"\n',
        encoding="utf-8",
    )
    transfer.chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'uv:%s\\n\' "$*" >> "${PUBLISH_CALL_LOG}"\n'
        '[[ "${PUBLISH_LEDGER_FAIL:-0}" != "1" ]]\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    gh = stub_bin / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\nprintf \'gh:%s\\n\' "$*" >> "${PUBLISH_CALL_LOG}"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{env['PATH']}",
            "PUBLISH_CALL_LOG": str(call_log),
        }
    )
    return publish, call_log, env


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_help_has_no_publish_side_effects(tmp_path: Path, help_flag: str) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)

    result = subprocess.run(
        (publish, help_flag),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("Usage: jobs/scripts/publish.sh\n")
    assert result.stderr == ""
    assert not call_log.exists()


@pytest.mark.parametrize("arguments", [("unexpected",), ("--help", "unexpected")])
def test_invalid_arguments_are_rejected_before_publish(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)

    result = subprocess.run(
        (publish, *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("Usage: jobs/scripts/publish.sh\n")
    assert not call_log.exists()


def test_no_argument_validates_ledger_before_upload(tmp_path: Path) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)

    result = subprocess.run(
        (publish,),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        (
            "uv:run baibai-engine position ledger "
            f"--db {publish.parents[2]}/stores/application/baibai.sqlite"
        ),
        "r2:push-app",
    ]
    assert result.stdout == "application DB uploaded\n"
    assert result.stderr == ""


def test_failed_ledger_preflight_stops_before_publish(tmp_path: Path) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)
    env["PUBLISH_LEDGER_FAIL"] = "1"

    result = subprocess.run(
        (publish,),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        (
            "uv:run baibai-engine position ledger "
            f"--db {publish.parents[2]}/stores/application/baibai.sqlite"
        )
    ]
    assert result.stdout == ""


def test_failed_application_transfer_does_not_report_success(tmp_path: Path) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)
    transfer = publish.parent / "r2_transfer.sh"
    with transfer.open("a") as stream:
        stream.write("exit 1\n")
    result = subprocess.run(
        (publish,),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    calls = call_log.read_text().splitlines()
    assert "r2:push-app" in calls
    assert not any(call.startswith("gh:") for call in calls)


@pytest.mark.parametrize("command", ["uv", "gh"])
def test_publish_uses_its_own_repository_from_another_directory(
    tmp_path: Path, command: str
) -> None:
    publish, call_log, env = _isolated_publish_script(tmp_path)
    elsewhere = tmp_path / "another-repo"
    elsewhere.mkdir()
    cwd_log = tmp_path / "cwd.log"
    stub = tmp_path / "bin" / command
    with stub.open("a") as stream:
        stream.write('pwd > "${PUBLISH_CWD_LOG}"\n')
    env["PUBLISH_CWD_LOG"] = str(cwd_log)

    result = subprocess.run(
        (publish,),
        cwd=elsewhere,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert Path(cwd_log.read_text().strip()) == publish.parents[2]
    assert call_log.read_text().splitlines()[-1].startswith("gh:workflow run ")
