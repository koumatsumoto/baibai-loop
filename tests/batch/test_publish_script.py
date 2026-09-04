from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_SCRIPT = ROOT / "batch" / "scripts" / "publish.sh"


def _isolated_publish_script(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    scripts = root / "batch" / "scripts"
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
    assert result.stdout.startswith("Usage: batch/scripts/publish.sh\n")
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
    assert result.stderr.startswith("Usage: batch/scripts/publish.sh\n")
    assert not call_log.exists()


def test_no_argument_keeps_upload_and_dispatch_order(tmp_path: Path) -> None:
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
        "gh:workflow run cloud-materialize.yml --ref main",
    ]
    assert result.stdout == "application DB uploaded; cloud-materialize dispatch requested\n"
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
