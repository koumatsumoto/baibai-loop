#!/usr/bin/env python3
"""Reclaim only unused SDKs on a fresh, credential-free GitHub Ubuntu 24.04 VM."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

# Exact installer roots in actions/runner-images/images/ubuntu/scripts/build:
# install-android-sdk.sh, install-dotnetcore-sdk.sh, install-swift.sh, install-haskell.sh.
# No user-supplied paths, package-manager purge, Docker cleanup, or tool-cache deletion.
_SDK_ROOTS = (
    "/usr/local/lib/android",
    "/usr/share/dotnet",
    "/usr/share/swift",
    "/usr/local/.ghcup",
)
_REQUIRED_ENV = {
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "Linux",
    "ImageOS": "ubuntu24",
    "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_RUN_ATTEMPT": "1",
}
_CREDENTIALS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CLOUDFLARE_API_TOKEN",
)


def _targets(env: Mapping[str, str], root: Path) -> tuple[Path, ...]:
    if any(env.get(name) != value for name, value in _REQUIRED_ENV.items()):
        raise ValueError("requires the trusted one-shot GitHub-hosted Ubuntu 24.04 workflow")
    if any(env.get(name) for name in _CREDENTIALS):
        raise ValueError("SDK preparation must run before credential injection")
    workspace = env.get("GITHUB_WORKSPACE")
    if workspace != "/home/runner/work/baibai-loop/baibai-loop":
        raise ValueError("unexpected hosted checkout location")
    release = root / "etc/os-release"
    values = {}
    for line in release.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
        raise ValueError("requires Ubuntu 24.04")
    protected = [root / workspace.lstrip("/"), Path(sys.executable).resolve()]
    for name in ("RUNNER_TEMP", "RUNNER_TOOL_CACHE"):
        if env.get(name):
            protected.append(root / env[name].lstrip("/"))
    for command in ("python", "python3", "uv", "node"):
        executable = shutil.which(command)
        if executable:
            protected.append(Path(executable).resolve())
    targets = []
    for raw in _SDK_ROOTS:
        target = root / raw.lstrip("/")
        # Validate all targets before deleting any. SDK-internal symlinks are
        # unlinked by rm, never followed; target/ancestor symlinks are forbidden.
        if target.resolve() != target or target.is_symlink():
            raise ValueError("SDK root or ancestor is a symlink")
        if any(path == target or path.is_relative_to(target) for path in protected):
            raise ValueError("SDK root contains a protected runtime or checkout")
        if target.exists():
            if not target.is_dir() or target.is_mount():
                raise ValueError("SDK root is not an ordinary directory")
            targets.append(target)
    return tuple(targets)


def _remove_sdk(target: Path) -> None:
    subprocess.run(  # nosec B603 B607
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/rm",
            "--recursive",
            "--force",
            "--one-file-system",
            "--",
            str(target),
        ],
        check=True,
    )


def prepare(
    env: Mapping[str, str],
    *,
    root: Path = Path("/"),
    remove: Callable[[Path], None] = _remove_sdk,
) -> dict[str, object]:
    """The root/remover seams are for fake-filesystem tests, not command-line inputs."""
    targets = _targets(env, root)
    workspace = root / env["GITHUB_WORKSPACE"].lstrip("/")
    before = shutil.disk_usage(workspace).free
    removed = []
    for target in targets:
        if target.resolve() != target or target.is_symlink() or target.is_mount():
            raise ValueError("SDK root changed before removal")
        remove(target)
        if target.exists() or target.is_symlink():
            raise ValueError("SDK removal incomplete")
        removed.append("/" + str(target.relative_to(root)))
    after = shutil.disk_usage(workspace).free
    return {
        "removed_sdk_roots": removed,
        "free_bytes_before": before,
        "free_bytes_after": after,
        "free_bytes_delta": after - before,
        "workspace_device": workspace.stat().st_dev,
        "capacity_approved": False,
    }


def main() -> int:
    if len(sys.argv) != 1 or platform.system() != "Linux":
        raise ValueError("no arguments accepted; Linux hosted runner only")
    print(json.dumps(prepare(os.environ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
