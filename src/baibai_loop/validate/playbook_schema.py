"""Per-playbook schema loader for research markdown body sections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from baibai_loop.yaml_io import safe_load

from .errors import ValidationFinding


class PlaybookSchemaError(ValueError):
    """Raised when a playbook schema YAML is malformed."""


@dataclass(frozen=True, slots=True)
class PlaybookBodySection:
    title_pattern: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class PlaybookSchema:
    name: str
    body_sections: tuple[PlaybookBodySection, ...]


def discover_playbook_schemas(root: Path) -> set[str]:
    """Return the set of playbook ids with a structured playbook schema."""
    if not root.exists():
        return set()
    return {path.parent.name for path in root.glob("*/body-schema.yaml") if path.is_file()}


def load_playbook_schema(root: Path, playbook: str) -> PlaybookSchema:
    """Load a playbook body schema and parse it into a PlaybookSchema."""
    schema_path = root / playbook / "body-schema.yaml"
    if not schema_path.exists():
        raise FileNotFoundError(f"playbook schema not found: {schema_path}")
    raw = safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PlaybookSchemaError(f"schema root must be a mapping: {schema_path}")
    sections_raw = raw.get("body_sections")
    if not isinstance(sections_raw, list):
        raise PlaybookSchemaError(f"`body_sections` must be a list: {schema_path}")
    sections: list[PlaybookBodySection] = []
    for entry in sections_raw:
        if not isinstance(entry, dict):
            raise PlaybookSchemaError(f"section entry must be a mapping: {schema_path}")
        title_pattern = entry.get("title_pattern")
        if not isinstance(title_pattern, str) or not title_pattern:
            raise PlaybookSchemaError(f"`title_pattern` must be a non-empty string: {schema_path}")
        try:
            re.compile(title_pattern)
        except re.error as exc:
            raise PlaybookSchemaError(
                f"invalid `title_pattern` regex {title_pattern!r}: {exc}"
            ) from exc
        required_value = entry.get("required", True)
        if not isinstance(required_value, bool):
            raise PlaybookSchemaError(f"`required` must be a boolean: {schema_path}")
        sections.append(PlaybookBodySection(title_pattern=title_pattern, required=required_value))
    return PlaybookSchema(name=playbook, body_sections=tuple(sections))


def validate_research_body(
    path: Path, body: str, schema: PlaybookSchema
) -> list[ValidationFinding]:
    """Check that body markdown contains all required sections defined in schema."""
    findings: list[ValidationFinding] = []
    headings = _extract_h2_headings(body)
    for section in schema.body_sections:
        if not section.required:
            continue
        pattern = re.compile(section.title_pattern)
        if not any(pattern.search(heading) for heading in headings):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.missing-section",
                    message=(
                        f"required section missing for playbook {schema.name!r}: "
                        f"pattern {section.title_pattern!r}"
                    ),
                    location=f"playbook_id:{schema.name}",
                )
            )
    return findings


def _extract_h2_headings(body: str) -> list[str]:
    return [line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("## ")]
