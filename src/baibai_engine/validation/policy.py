"""Validate portfolio policy documentation."""

from __future__ import annotations

from pathlib import Path

from baibai_engine.foundation.errors import ValidationFinding


def discover_policy_files(root: Path) -> list[Path]:
    """Return portfolio policy docs."""
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.md") if path.is_file())


def validate_policy_file(path: Path) -> list[ValidationFinding]:
    """Validate that the policy document is a readable Markdown document."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="policy.io",
                message=f"failed to read file: {exc}",
            )
        ]
    if "# Portfolio management" not in text:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="policy.heading",
                message="portfolio policy document must contain '# Portfolio management'",
            )
        ]
    if "position/policy.py" not in text:
        return [
            ValidationFinding(
                severity="warning",
                target=path,
                code="policy.code-config-note",
                message="portfolio policy should point concrete thresholds to position/policy.py",
            )
        ]
    return []
