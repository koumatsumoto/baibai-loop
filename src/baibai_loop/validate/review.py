from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .domain import repo_root_for, repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "review.json"
RETRO_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "records" / "_schemas" / "retro-monthly.json"
)
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Outcome",
    "Hypothesis check",
    "Process check",
    "Lessons",
    "Next actions",
)
REQUIRED_RETRO_SECTIONS: tuple[str, ...] = (
    "Trade 集計",
    "失敗分類の集計",
    "成功分類の集計",
    "Missed opportunity / screening false negative tracking の分析",
    "Macro regime gate 判定精度",
    "Playbook 改訂判断",
    "次周回の運用変更点",
)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _load_schema() -> dict[str, Any]:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return raw


def _classifications_from_schema(schema: dict[str, Any]) -> tuple[str, ...]:
    # schema を単一の source of truth として扱う。constant 側を更新し忘れる drift を避ける。
    enum = schema["properties"]["classification"]["enum"]
    return tuple(str(value) for value in enum)


_SCHEMA = _load_schema()
_RETRO_SCHEMA = json.loads(RETRO_SCHEMA_PATH.read_text(encoding="utf-8"))
if not isinstance(_RETRO_SCHEMA, dict):
    raise RuntimeError(f"unexpected schema root: {RETRO_SCHEMA_PATH}")
Draft202012Validator.check_schema(_RETRO_SCHEMA)
_VALIDATOR = Draft202012Validator(_SCHEMA)
_RETRO_VALIDATOR = Draft202012Validator(_RETRO_SCHEMA)
KNOWN_CLASSIFICATIONS: tuple[str, ...] = _classifications_from_schema(_SCHEMA)


def discover_review_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and (
            (path.suffix == ".md" and path.name != "template.md")
            or (path.suffix in {".yaml", ".yml"})
        )
    )


def validate_review_file(path: Path) -> list[ValidationFinding]:
    if path.suffix in {".yaml", ".yml"}:
        return _validate_review_scan_file(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.no-front-matter",
                message="review markdown must start with YAML front matter",
            )
        ]
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.front-matter-non-mapping",
                message="review front matter must be a mapping",
            )
        ]
    validator = _RETRO_VALIDATOR if path.name.startswith("retro-") else _VALIDATOR
    code_prefix = "retro" if path.name.startswith("retro-") else "review"
    required_sections = (
        REQUIRED_RETRO_SECTIONS if path.name.startswith("retro-") else REQUIRED_SECTIONS
    )

    findings: list[ValidationFinding] = []
    for error in validator.iter_errors(front):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"{code_prefix}.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    if code_prefix == "review":
        findings.extend(_check_review_repository_refs(path, front))
    body = match.group(2)
    for section in required_sections:
        if not re.search(rf"^##\s+{re.escape(section)}\s*$", body, flags=re.MULTILINE):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code=f"{code_prefix}.missing-section",
                    message=f"required section missing: {section}",
                    location=section,
                )
            )
    return findings


def _check_review_repository_refs(
    path: Path,
    front: Mapping[str, Any],
) -> list[ValidationFinding]:
    specs = {
        "research_ref": ("records/05-research/", (".md",)),
        "trade_ref": ("records/06-trades/", (".md",)),
    }
    root = repo_root_for(path)
    findings: list[ValidationFinding] = []
    for field, (prefix, suffixes) in specs.items():
        value = front.get(field)
        if value is None:
            continue
        error = repository_ref_error(value, root=root)
        if error is not None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.repository-ref",
                    message=error,
                    location=field,
                )
            )
            continue
        assert isinstance(value, str)
        ref_path = resolve_repository_ref(root, value)
        if not value.startswith(prefix):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.repository-ref",
                    message=f"{field} must point under {prefix}",
                    location=field,
                )
            )
            continue
        if ref_path.suffix not in suffixes:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.repository-ref",
                    message=f"{field} must use suffix {suffixes}",
                    location=field,
                )
            )
            continue
        if not ref_path.is_file():
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.repository-ref",
                    message=f"{field} referenced file does not exist: {value}",
                    location=field,
                )
            )
            continue
        if _markdown_front_matter(ref_path) is None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.repository-ref",
                    message=f"{field} referenced markdown must have YAML front matter",
                    location=field,
                )
            )
    return findings


def _markdown_front_matter(path: Path) -> Mapping[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return None
    try:
        payload = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _validate_review_scan_file(path: Path) -> list[ValidationFinding]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review-scan.parse",
                message=f"failed to read review scan: {exc}",
            )
        ]
    if not isinstance(raw, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review-scan.root",
                message="review scan YAML must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_check_scan_repository_refs(path, raw))
    if "screening-false-negative-scan" in path.parts:
        items = raw.get("items")
        if not isinstance(items, list):
            return [
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.items",
                    message="screening false negative scan requires items list",
                    location="items",
                )
            ]
        findings.extend(_check_false_negative_scan(path, raw, items))
    elif "missed-opportunity-scan" in path.parts:
        items = raw.get("items")
        if not isinstance(items, list):
            return [
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.items",
                    message="missed opportunity scan requires items list",
                    location="items",
                )
            ]
        findings.extend(_check_missed_opportunity_scan(path, items))
    return findings


def _check_scan_repository_refs(path: Path, scan: Mapping[str, Any]) -> list[ValidationFinding]:
    specs = {
        "market_data_ref": ("records/_market-data/", (".yaml", ".yml")),
        "universe_ref": ("records/_universe-snapshots/", (".yaml", ".yml")),
    }
    root = repo_root_for(path)
    findings: list[ValidationFinding] = []
    for field, (prefix, suffixes) in specs.items():
        value = scan.get(field)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=f"{field} must be a repository ref mapping",
                    location=field,
                )
            )
            continue
        ref = value.get("ref_path")
        error = repository_ref_error(ref, root=root)
        if error is not None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=error,
                    location=f"{field}.ref_path",
                )
            )
            continue
        assert isinstance(ref, str)
        ref_path = resolve_repository_ref(root, ref)
        if not ref.startswith(prefix):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=f"{field}.ref_path must point under {prefix}",
                    location=f"{field}.ref_path",
                )
            )
        if ref_path.suffix not in suffixes:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=f"{field}.ref_path must use YAML",
                    location=f"{field}.ref_path",
                )
            )
            continue
        if not ref_path.is_file():
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=f"referenced file does not exist: {ref}",
                    location=f"{field}.ref_path",
                )
            )
            continue
        try:
            loaded = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message=f"referenced file cannot be parsed: {exc}",
                    location=f"{field}.ref_path",
                )
            )
            continue
        if not isinstance(loaded, Mapping):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.repository-ref",
                    message="referenced YAML must be a mapping",
                    location=f"{field}.ref_path",
                )
            )
    return findings


def _check_false_negative_scan(
    path: Path,
    scan: dict[str, Any],
    items: list[Any],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if scan.get("start_price_basis") != "candidate_run_close_adjusted_close":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="review-scan.start-price-basis",
                message=(
                    "screening false negative scans must use "
                    "candidate_run_close_adjusted_close as the canonical start basis"
                ),
                location="start_price_basis",
            )
        )
    known_decisions = _decision_event_ids(_repo_root(path))
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.item",
                    message="scan item must be a mapping",
                    location=f"items[{index}]",
                )
            )
            continue
        if item.get("start_price_basis") != "candidate_run_close_adjusted_close":
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review-scan.item-start-price-basis",
                    message=(
                        "false negative scan item start price must be the original "
                        "candidate run close"
                    ),
                    location=f"items[{index}].start_price_basis",
                )
            )
        findings.extend(_check_scan_decision_anchor(path, item, known_decisions, index))
    return findings


def _check_missed_opportunity_scan(
    path: Path,
    items: list[Any],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    known_decisions = _decision_event_ids(_repo_root(path))
    for index, item in enumerate(items):
        if isinstance(item, dict):
            findings.extend(_check_scan_decision_anchor(path, item, known_decisions, index))
    return findings


def _check_scan_decision_anchor(
    path: Path,
    item: dict[str, Any],
    known_decisions: set[str],
    index: int,
) -> list[ValidationFinding]:
    decision_id = item.get("decision_event_id")
    if not isinstance(decision_id, str) or not decision_id:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review-scan.decision-event-id",
                message="scan items require decision_event_id anchor",
                location=f"items[{index}].decision_event_id",
            )
        ]
    if decision_id not in known_decisions:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review-scan.decision-event-missing",
                message="scan item decision_event_id must join to the decision register",
                location=f"items[{index}].decision_event_id",
            )
        ]
    return []


def _decision_event_ids(root: Path) -> set[str]:
    decisions: set[str] = set()
    for path in sorted((root / "records/_ledger/research-decisions").glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("decision_event_id"), str):
                decisions.add(item["decision_event_id"])
    return decisions


def _repo_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if (parent / "records").is_dir() and (parent / "src").is_dir():
            return parent
    return Path.cwd()


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
