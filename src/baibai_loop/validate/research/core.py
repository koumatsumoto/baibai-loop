"""Public entry points: schema validation and check orchestration."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.validate.errors import ValidationFinding
from baibai_loop.validate.playbook_schema import (
    PlaybookSchemaError,
    discover_playbook_schemas,
    load_playbook_schema,
    validate_research_body,
)

from .fields import _check_decision, _check_playbook, _check_ticker
from .macro_context import _check_macro_context_fit
from .payoff import _check_corporate_action_check, _check_payoff
from .preflight import _check_entry_preflight
from .refs import _check_reference_refs
from .shared import _format_path
from .sizing import _check_sizing_invariants

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "records" / "_schemas" / "research.json"
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def validate_research_file(
    path: Path,
    *,
    playbooks_root: Path | None = None,
    known_playbooks: frozenset[str] | None = None,
) -> list[ValidationFinding]:
    loaded = _load_research_document(path)
    if isinstance(loaded, list):
        return loaded
    front_matter, body = loaded
    return validate_research_parsed(
        path,
        front_matter,
        body,
        playbooks_root=playbooks_root,
        known_playbooks=known_playbooks,
    )


def validate_research_parsed(
    path: Path,
    front_matter: dict[str, object],
    body: str,
    *,
    playbooks_root: Path | None = None,
    known_playbooks: frozenset[str] | None = None,
) -> list[ValidationFinding]:
    playbook_root = playbooks_root or _default_playbook_root()
    if known_playbooks is None:
        known_playbooks = frozenset(discover_playbook_schemas(playbook_root))

    findings: list[ValidationFinding] = []
    findings.extend(_validate_schema(path, front_matter))
    findings.extend(_check_ticker(path, front_matter))
    findings.extend(_check_playbook(path, front_matter, known_playbooks))
    findings.extend(_check_decision(path, front_matter))
    findings.extend(_check_macro_context_fit(path, front_matter))
    findings.extend(_check_entry_preflight(path, front_matter))
    findings.extend(_check_sizing_invariants(path, front_matter))
    findings.extend(_check_corporate_action_check(path, front_matter))
    findings.extend(_check_payoff(path, front_matter))
    findings.extend(_check_reference_refs(path, front_matter))

    playbook_id = front_matter.get("playbook_id")
    if isinstance(playbook_id, str) and playbook_id in known_playbooks:
        try:
            schema = load_playbook_schema(playbook_root, playbook_id)
        except (FileNotFoundError, PlaybookSchemaError) as exc:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.playbook-schema",
                    message=str(exc),
                    location=f"playbook_id:{playbook_id}",
                )
            )
        else:
            findings.extend(validate_research_body(path, body, schema))
    return findings


def load_research_document(
    path: Path,
) -> tuple[dict[str, object], str] | list[ValidationFinding]:
    return _load_research_document(path)


def validate_research_collection(
    paths_with_front_matter: Sequence[tuple[Path, Mapping[str, object]]],
) -> list[ValidationFinding]:
    approved_by_sector: dict[str, list[Path]] = {}
    for path, front_matter in paths_with_front_matter:
        decision = front_matter.get("research_decision")
        if not isinstance(decision, Mapping) or decision.get("outcome") != "approved":
            continue
        sector = front_matter.get("sector_33")
        if isinstance(sector, str) and sector.strip():
            approved_by_sector.setdefault(sector, []).append(path)

    findings: list[ValidationFinding] = []
    for sector, paths in sorted(approved_by_sector.items()):
        if len(paths) < 3:
            continue
        for path in paths:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="research.sector-concentration",
                    message=(
                        f"3+ approved investment memos share sector_33={sector}; "
                        "review cumulative exposure"
                    ),
                    location="sector_33",
                )
            )
    return findings


def discover_research_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _load_research_document(
    path: Path,
) -> tuple[dict[str, object], str] | list[ValidationFinding]:
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
                message="investment memo markdown must start with `---` YAML front matter",
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
                message="investment memo front matter must be a mapping",
            )
        ]
    return front_matter, match.group(2)


def _default_playbook_root() -> Path:
    return Path(__file__).resolve().parents[4] / "records" / "_playbooks"


def _validate_schema(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(front_matter):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=(
                    "research.required"
                    if error.validator == "anyOf"
                    else f"research.{error.validator or 'invalid'}"
                ),
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings
