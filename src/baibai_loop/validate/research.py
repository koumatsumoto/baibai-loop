"""Validate research markdown front matter and (via playbook schema) body sections.

PR 1-C 時点では `decision` / `macro_gate_override` は research front matter に
まだ存在しない。これらの field は Phase 2 で追加するため、本モジュールでは
現行の必須 front matter のみを検証する。本文 section の検証は playbook 別
schema (`playbooks/<name>.schema.yaml`) に分離する。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import ValidationFinding
from .playbook_schema import (
    PlaybookSchemaError,
    discover_playbook_schemas,
    load_playbook_schema,
    validate_research_body,
)

KNOWN_MACRO_GATES: tuple[str, ...] = ("tailwind", "neutral", "headwind")
REQUIRED_FRONT_MATTER: tuple[str, ...] = (
    "ticker",
    "name",
    "playbook",
    "screened_ref",
    "view_ref",
    "brief_refs",
    "ai-draft",
    "published_at",
    "tradable_at",
    "macro_gate",
    "valuation",
)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")


def validate_research_file(
    path: Path, *, playbooks_root: Path | None = None
) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.no-front-matter",
                message="research markdown must start with `---` YAML front matter",
            )
        ]
    try:
        front_matter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front_matter, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.front-matter-non-mapping",
                message="research front matter must be a mapping",
            )
        ]
    body = match.group(2)
    playbook_root = playbooks_root or _default_playbook_root()
    known_playbooks = frozenset(discover_playbook_schemas(playbook_root))
    findings: list[ValidationFinding] = []
    findings.extend(_validate_front_matter(path, front_matter, known_playbooks))
    playbook = front_matter.get("playbook")
    if isinstance(playbook, str) and playbook in known_playbooks:
        try:
            schema = load_playbook_schema(playbook_root, playbook)
        except PlaybookSchemaError as exc:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-playbook-schema",
                    message=str(exc),
                    location=f"playbook:{playbook}",
                )
            )
        else:
            findings.extend(validate_research_body(path, body, schema))
    return findings


def discover_research_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _default_playbook_root() -> Path:
    return Path(__file__).resolve().parents[3] / "playbooks"


def _validate_front_matter(
    path: Path,
    front_matter: dict[str, object],
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in REQUIRED_FRONT_MATTER:
        if field not in front_matter:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.missing-field",
                    message=f"required front matter field missing: {field}",
                    location=field,
                )
            )
    ticker = front_matter.get("ticker")
    if isinstance(ticker, str) and not _TICKER_PATTERN.match(ticker):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-ticker",
                message=f"ticker must be 4-char alphanumeric: {ticker!r}",
                location="ticker",
            )
        )
    playbook = front_matter.get("playbook")
    if isinstance(playbook, str) and playbook not in known_playbooks:
        known_sorted = sorted(known_playbooks)
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-playbook",
                message=(
                    f"playbook {playbook!r} has no schema in playbooks/; known: {known_sorted}"
                ),
                location="playbook",
            )
        )
    macro_gate = front_matter.get("macro_gate")
    if isinstance(macro_gate, str) and macro_gate not in KNOWN_MACRO_GATES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-macro-gate",
                message=(
                    f"macro_gate must be one of {list(KNOWN_MACRO_GATES)}, got {macro_gate!r}"
                ),
                location="macro_gate",
            )
        )
    screened_ref = front_matter.get("screened_ref")
    if isinstance(screened_ref, str) and not screened_ref.endswith(".yaml"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.screened-ref-not-yaml",
                message="screened_ref must end with .yaml (PR 1-A: screened is YAML)",
                location="screened_ref",
            )
        )
    return findings
