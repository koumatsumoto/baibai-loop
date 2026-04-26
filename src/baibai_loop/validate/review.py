from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import ValidationFinding

KNOWN_CLASSIFICATIONS: tuple[str, ...] = (
    "success",
    "failure",
    "invalidated",
    "inconclusive",
)
REQUIRED_FRONT_MATTER: tuple[str, ...] = ("trade_ref", "classification", "verified_at")
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Outcome",
    "Hypothesis check",
    "Process check",
    "Lessons",
    "Next actions",
)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def discover_review_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.rglob("*.md") if path.is_file() and path.name != "template.md"
    )


def validate_review_file(path: Path) -> list[ValidationFinding]:
    match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.no-front-matter",
                message="review markdown must start with YAML front matter",
            )
        ]
    front = yaml.safe_load(match.group(1))
    if not isinstance(front, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.front-matter-non-mapping",
                message="review front matter must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for field in REQUIRED_FRONT_MATTER:
        if field not in front:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.missing-field",
                    message=f"required front matter field missing: {field}",
                    location=field,
                )
            )
    classification = front.get("classification")
    if isinstance(classification, str) and classification not in KNOWN_CLASSIFICATIONS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="review.invalid-classification",
                message=(
                    "classification must be one of "
                    f"{list(KNOWN_CLASSIFICATIONS)}, got {classification!r}"
                ),
                location="classification",
            )
        )
    body = match.group(2)
    for section in REQUIRED_SECTIONS:
        if not re.search(rf"^##\s+{re.escape(section)}\s*$", body, flags=re.MULTILINE):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.missing-section",
                    message=f"required section missing: {section}",
                    location=section,
                )
            )
    return findings
