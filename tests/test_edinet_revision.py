from __future__ import annotations

import re
from pathlib import Path

import pytest

from baibai_engine.screening.edinet_revision import (
    EXTRACTION_ARTIFACT_MANIFEST,
    compute_extractor_revision,
)


def _artifacts() -> dict[str, bytes]:
    return {path: f"source:{path}".encode() for path in EXTRACTION_ARTIFACT_MANIFEST}


def test_extractor_revision_manifest_covers_mapping_parser_and_composition() -> None:
    screening_root = Path(__file__).resolve().parents[1] / "src" / "baibai_engine" / "screening"
    expected_screening = sorted(
        f"screening/{path.relative_to(screening_root).as_posix()}"
        for path in screening_root.rglob("*.py")
    )
    assert ("market/ticker.py", *expected_screening) == EXTRACTION_ARTIFACT_MANIFEST
    assert re.fullmatch(r"[0-9a-f]{64}", compute_extractor_revision())


def test_extractor_revision_changes_when_any_artifact_changes() -> None:
    original = _artifacts()
    original_revision = compute_extractor_revision(original)

    for path in EXTRACTION_ARTIFACT_MANIFEST:
        changed = {**original, path: original[path] + b"\nchange"}
        assert compute_extractor_revision(changed) != original_revision


def test_extractor_revision_rejects_manifest_drift() -> None:
    artifacts = _artifacts()
    artifacts.pop(EXTRACTION_ARTIFACT_MANIFEST[0])
    with pytest.raises(ValueError, match="manifest mismatch"):
        compute_extractor_revision(artifacts)
