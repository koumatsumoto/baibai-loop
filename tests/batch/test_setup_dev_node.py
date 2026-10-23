from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("installed", ["24.21.0", "22.23.4", "24.22.1"])
def test_bootstrap_checks_exact_node_before_dependency_install(
    tmp_path: Path, installed: str
) -> None:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copyfile(ROOT / "tools/setup-dev.sh", repo / "tools/setup-dev.sh")
    (repo / ".node-version").write_text("24.22.1\n")
    (repo / ".python-version").write_text("3.14.0\n")
    (repo / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==0.10.0"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    commands = {
        "node": f"#!/bin/sh\nprintf '%s\\n' '{installed}'\n",
        "npm": "#!/bin/sh\necho unexpected-npm >&2\nexit 99\n",
        "uv": (
            '#!/bin/sh\nif [ "$1" = --version ]; then echo "uv 0.10.0"; exit 0; fi\n'
            "echo accepted-node-install-sentinel >&2\nexit 42\n"
        ),
    }
    for name, content in commands.items():
        executable = bin_dir / name
        executable.write_text(content)
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(repo / "tools/setup-dev.sh")],
        env={**os.environ, "PATH": f"{bin_dir}:{os.defpath}"},
        capture_output=True,
        text=True,
        check=False,
    )
    if installed == "24.22.1":
        assert result.returncode == 42
        assert "accepted-node-install-sentinel" in result.stderr
        assert "Provision Node.js" not in result.stderr
    else:
        assert result.returncode == 1
        assert "Provision Node.js 24.22.1" in result.stderr
        assert "accepted-node-install-sentinel" not in result.stderr
        assert not (repo / ".cache/dev/env.sh").exists()
    assert "unexpected-npm" not in result.stderr
