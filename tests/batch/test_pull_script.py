from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def test_pull_prepares_market_before_reporting_success(tmp_path: Path) -> None:
    scripts = tmp_path / "batch" / "scripts"
    scripts.mkdir(parents=True)
    pull = scripts / "pull.sh"
    shutil.copy2(Path(__file__).resolve().parents[2] / "batch/scripts/pull.sh", pull)
    transfer = scripts / "r2_transfer.sh"
    transfer.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$1" >> "$PULL_CALL_LOG"\n'
        'if [[ "$1" == "$PULL_FAIL_STAGE" ]]; then exit 7; fi\n',
        encoding="utf-8",
    )
    transfer.chmod(0o755)
    calls = tmp_path / "calls.log"
    for failure, expected in (
        ("", ["pull-machine", "hydrate-market"]),
        ("pull-machine", ["pull-machine"]),
        ("hydrate-market", ["pull-machine", "hydrate-market"]),
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
