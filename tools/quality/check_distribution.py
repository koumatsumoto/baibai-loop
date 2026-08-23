"""Validate source/binary archives and an isolated wheel installation."""

from __future__ import annotations

import argparse
import importlib

# Only fixed venv-local console-script paths and the literal --help are executed.
import subprocess  # nosec B404
import sys
import tarfile
import zipfile
from importlib import resources
from pathlib import Path

_PACKAGES = ("baibai_engine", "baibai_web", "baibai_batch")
_RUNTIME_MODULES = (
    "baibai_batch.storage.lake_publish",
    "baibai_batch.storage.publish_market_lake",
)
_WHEEL_REQUIRED = {
    "baibai_engine/__init__.py",
    "baibai_engine/macro/indicators/schema.sql",
    "baibai_engine/macro/indicators/registry/us.yaml",
    "baibai_engine/macro/indicators/registry/japan.yaml",
    "baibai_engine/macro/indicators/registry/global.yaml",
    "baibai_engine/macro/indicators/registry/derived.yaml",
    "baibai_engine/macro/indicators/providers/pmi_release_urls.yaml",
    "baibai_web/__init__.py",
    "baibai_batch/__init__.py",
}
_SDIST_REQUIRED = {
    "engine/src/baibai_engine/__init__.py",
    "engine/src/baibai_engine/macro/indicators/schema.sql",
    "engine/src/baibai_engine/macro/indicators/registry/us.yaml",
    "engine/src/baibai_engine/macro/indicators/providers/pmi_release_urls.yaml",
    "web/backend/src/baibai_web/__init__.py",
    "batch/src/baibai_batch/__init__.py",
}
_FORBIDDEN_PARTS = {"data", "method", "reports", "stores"}


class DistributionError(RuntimeError):
    """A built artifact does not implement the single-distribution contract."""


def _assert_archive(names: set[str], required: set[str], *, has_root_prefix: bool) -> None:
    normalized = {
        "/".join(name.split("/")[1:]) if has_root_prefix and "/" in name else name for name in names
    }
    missing = sorted(required.difference(normalized))
    if missing:
        raise DistributionError(f"archive is missing required files: {', '.join(missing)}")
    forbidden = sorted(
        name
        for name in normalized
        if _FORBIDDEN_PARTS.intersection(Path(name).parts) or name.endswith(".sqlite")
    )
    if forbidden:
        raise DistributionError(f"runtime/evidence files leaked into archive: {forbidden[0]}")


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        _assert_archive(set(archive.namelist()), _WHEEL_REQUIRED, has_root_prefix=False)


def check_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        _assert_archive(set(archive.getnames()), _SDIST_REQUIRED, has_root_prefix=True)


def check_installed() -> None:
    for package in (*_PACKAGES, *_RUNTIME_MODULES):
        importlib.import_module(package)

    resource_checks = (
        ("baibai_engine.macro.indicators", "schema.sql"),
        ("baibai_engine.macro.indicators.registry", "us.yaml"),
        ("baibai_engine.macro.indicators.providers", "pmi_release_urls.yaml"),
    )
    for package, filename in resource_checks:
        content = resources.files(package).joinpath(filename).read_bytes()
        if not content:
            raise DistributionError(f"installed package resource is empty: {package}/{filename}")

    scripts = ("baibai-engine", "baibai-web", "baibai-batch")
    for script in scripts:
        executable = Path(sys.executable).parent / script
        # The executable is constrained to the active venv's bin directory.
        completed = subprocess.run(  # nosec B603
            [str(executable), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise DistributionError(
                f"installed {script} --help failed ({completed.returncode}): {completed.stderr}"
            )

    for module in _RUNTIME_MODULES:
        completed = subprocess.run(  # nosec B603
            [sys.executable, "-m", module, "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise DistributionError(
                f"installed python -m {module} --help failed "
                f"({completed.returncode}): {completed.stderr}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--installed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not any((args.wheel, args.sdist, args.installed)):
        _parser().error("select --wheel, --sdist, and/or --installed")
    try:
        if args.wheel:
            check_wheel(args.wheel)
        if args.sdist:
            check_sdist(args.sdist)
        if args.installed:
            check_installed()
    except (DistributionError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
