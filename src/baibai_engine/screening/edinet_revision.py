"""Deterministic revision for the sources that produce EDINET metric rows."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator, Mapping, Sequence
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

EXTRACTION_ARTIFACT_CONTRACT = "edinet-metric-extractor-v1"

_ROOT_PACKAGE = "baibai_engine"
# `extract-edinet-metrics` is implemented by `extract_edinet_metrics_command`, so the
# module holding it is where the extraction path starts. That module holds nothing else:
# the closure below is what decides when every stored metric row is discarded, and a
# module shared with the other cache commands would drag their J-Quants, JPX and coverage
# dependencies into it.
_ENTRY_MODULES = ("baibai_engine.screening.cli.edinet_extract",)
# Only these subtrees can decide which filings are selected or what values their rows
# carry. Everything else the entry reaches (stdlib, requests, market SQLite plumbing)
# either has no say in the values or is pinned by the environment rather than the tree.
_TRACKED_PREFIXES = ("baibai_engine.screening", "baibai_engine.market.ticker")


def _package_root() -> Traversable:
    return resources.files(_ROOT_PACKAGE)


def _artifact(path: str) -> Traversable:
    return _package_root().joinpath(*PurePosixPath(path).parts)


def _resolve_module(module: str) -> tuple[str, bool] | None:
    """Return the bundled artifact path for a module and whether it is a package."""
    if module == _ROOT_PACKAGE:
        stem = ""
    elif module.startswith(f"{_ROOT_PACKAGE}."):
        stem = module[len(_ROOT_PACKAGE) + 1 :].replace(".", "/")
    else:
        return None
    if stem:
        module_file = f"{stem}.py"
        if _artifact(module_file).is_file():
            return module_file, False
    package_file = f"{stem}/__init__.py" if stem else "__init__.py"
    if _artifact(package_file).is_file():
        return package_file, True
    return None


def _is_tracked(module: str) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in _TRACKED_PREFIXES)


def _imported_modules(source: bytes, *, module: str, is_package: bool) -> Iterator[str]:
    """Yield every module name an import statement in `source` can refer to.

    `from pkg import name` is ambiguous between an attribute and a submodule, so both
    `pkg` and `pkg.name` are yielded; the names that resolve to no bundled file are
    dropped when the closure tries to resolve them.
    """
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                for alias in node.names:
                    yield alias.name
            case ast.ImportFrom():
                if node.level:
                    base = package
                    for _ in range(node.level - 1):
                        base = base.rpartition(".")[0]
                    absolute = f"{base}.{node.module}" if node.module else base
                else:
                    absolute = node.module or ""
                if not absolute:
                    continue
                yield absolute
                for alias in node.names:
                    yield f"{absolute}.{alias.name}"


@cache
def extraction_artifact_manifest() -> tuple[str, ...]:
    """Bundled sources reachable from the extraction entry point, sorted by path.

    The revision has to change exactly when a metric row's value could change, so the
    manifest is the import closure of the extraction entry within the tracked subtrees,
    derived from the parsed import statements. Deriving it keeps the set following the
    code when the extraction path gains or drops a dependency. A wider set makes every
    unrelated screening edit invalidate the reusable baseline and force a full
    re-download; a narrower one lets a stale row survive a change that would move it.
    """
    artifacts: set[str] = set()
    visited: set[str] = set()
    pending = list(_ENTRY_MODULES)
    while pending:
        module = pending.pop()
        if module in visited or not _is_tracked(module):
            continue
        visited.add(module)
        resolved = _resolve_module(module)
        if resolved is None:
            continue
        path, is_package = resolved
        artifacts.add(path)
        source = _artifact(path).read_bytes()
        pending.extend(_imported_modules(source, module=module, is_package=is_package))
    return tuple(sorted(artifacts))


def has_hard_metric_failure(reasons: Sequence[str]) -> bool:
    """Return whether parser provenance means no reusable metric was produced."""
    return any(
        reason == "csv_parse_failed" or reason.startswith("csv_parse_failed:") for reason in reasons
    )


def compute_extractor_revision(artifacts: Mapping[str, bytes] | None = None) -> str:
    """Hash the contract and exact bundled sources that affect metric rows."""
    manifest = extraction_artifact_manifest()
    if artifacts is None:
        artifacts = {path: _artifact(path).read_bytes() for path in manifest}
    expected = set(manifest)
    if set(artifacts) != expected:
        missing = sorted(expected - set(artifacts))
        extra = sorted(set(artifacts) - expected)
        raise ValueError(
            f"EDINET extraction artifact manifest mismatch: missing={missing} extra={extra}"
        )
    digest = hashlib.sha256()
    digest.update(EXTRACTION_ARTIFACT_CONTRACT.encode("utf-8"))
    digest.update(b"\0")
    for path in manifest:
        content = artifacts[path]
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


__all__ = [
    "EXTRACTION_ARTIFACT_CONTRACT",
    "compute_extractor_revision",
    "extraction_artifact_manifest",
    "has_hard_metric_failure",
]
