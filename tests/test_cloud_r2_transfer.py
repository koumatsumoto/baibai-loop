from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFER_SCRIPT = REPO_ROOT / "tools/cloud/r2_transfer.sh"


def _fake_aws(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "aws.log"
    executable = bin_dir / "aws"
    executable.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, log


def _environment(bin_dir: Path, log: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
    }


def test_upload_serving_replaces_views_appends_history_and_writes_meta_last(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/dashboard.json").write_text("{}", encoding="utf-8")
    (output / "views/meta.json").write_text("{}", encoding="utf-8")
    (output / "history/select").mkdir(parents=True)
    (output / "history/select/2026-07-21.json").write_text("{}", encoding="utf-8")
    (output / "history/candidate-pool").mkdir(parents=True)
    (output / "history/candidate-pool/2026-07-21.json").write_text("{}", encoding="utf-8")

    subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 4
    assert commands[0].startswith("s3 sync ")
    assert "s3://baibai-serving/views/" in commands[0]
    assert "--delete --exclude meta.json" in commands[0]
    assert "--delete" not in commands[1]
    assert "--delete" not in commands[2]
    assert commands[3].startswith("s3 cp ")
    assert commands[3].endswith(
        "s3://baibai-serving/views/meta.json --endpoint-url "
        "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
    )


def test_upload_serving_rejects_an_export_without_meta(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "meta.json is missing" in completed.stderr
    assert not log.exists()
