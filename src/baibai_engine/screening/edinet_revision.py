"""Deterministic revision for artifacts that define EDINET metric output."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path, PurePosixPath

EXTRACTION_ARTIFACT_CONTRACT = "edinet-metric-extractor-v1"


def _build_artifact_manifest() -> tuple[str, ...]:
    """Cover every screening helper plus the shared ticker normalization contract."""
    screening_root = Path(__file__).resolve().parent
    screening_artifacts = sorted(
        f"screening/{path.relative_to(screening_root).as_posix()}"
        for path in screening_root.rglob("*.py")
    )
    return ("market/ticker.py", *screening_artifacts)


EXTRACTION_ARTIFACT_MANIFEST = _build_artifact_manifest()


def has_hard_metric_failure(reasons: Sequence[str]) -> bool:
    """Return whether parser provenance means no reusable metric was produced."""
    return any(
        reason == "csv_parse_failed" or reason.startswith("csv_parse_failed:") for reason in reasons
    )


def compute_extractor_revision(artifacts: Mapping[str, bytes] | None = None) -> str:
    """Hash the contract and exact bundled sources that affect metric rows."""
    if artifacts is None:
        package_root = resources.files("baibai_engine")
        artifacts = {
            path: package_root.joinpath(*PurePosixPath(path).parts).read_bytes()
            for path in EXTRACTION_ARTIFACT_MANIFEST
        }
    expected = set(EXTRACTION_ARTIFACT_MANIFEST)
    if set(artifacts) != expected:
        missing = sorted(expected - set(artifacts))
        extra = sorted(set(artifacts) - expected)
        raise ValueError(
            f"EDINET extraction artifact manifest mismatch: missing={missing} extra={extra}"
        )
    digest = hashlib.sha256()
    digest.update(EXTRACTION_ARTIFACT_CONTRACT.encode("utf-8"))
    digest.update(b"\0")
    for path in EXTRACTION_ARTIFACT_MANIFEST:
        content = artifacts[path]
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


__all__ = [
    "EXTRACTION_ARTIFACT_CONTRACT",
    "EXTRACTION_ARTIFACT_MANIFEST",
    "compute_extractor_revision",
    "has_hard_metric_failure",
]
