"""Validate candidates YAML artefacts against the central jsonschema.

candidates は YAML 正本である。R6 のコア schema として
`records/_schemas/candidates.json` で構造を中央集約し、本モジュールはその schema
で artefact を検証する。playbook 別 schema (本文 section 構造) は本
モジュールの対象外。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .domain import repo_root_for, repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "candidates.json"
_REMOVED_ROOT_FIELDS = frozenset(
    {
        "screening_rules_snapshot",
        "metric_catalog_snapshot",
        "_".join(("policy", "snapshot")),
        "cache_manifest_hash",
        "_".join(("universe", "snapshot", "ref")),
        "content_" + "sha256",
        "row_" + "sha256",
    }
)
_REMOVED_REFERENCE_FIELDS = frozenset(
    {
        "playbook_snapshot",
        "policy_snapshot",
        "portfolio_exposure_snapshot_ref",
        "calendars_snapshot",
        "universe_snapshot_ref",
        "input_snapshots",
        "screening_rules_snapshot",
        "metric_catalog_snapshot",
        "cache_manifest_hash",
        "snapshot_path",
        "latest_snapshot",
    }
)


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_candidates_file(path: Path) -> list[ValidationFinding]:
    """Validate a single candidates YAML file and return all findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.io",
                message=f"failed to read file: {exc}",
            )
        ]
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.invalid-yaml",
                message=f"YAML parse failed: {exc}",
            )
        ]
    if not isinstance(document, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.non-mapping",
                message="candidates YAML root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        validator_keyword = error.validator or "invalid"
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"candidates.{validator_keyword}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    findings.extend(_check_removed_root_fields(path, document))
    findings.extend(_check_removed_hash_fields(path, document))
    findings.extend(_check_universe_ref(path, document, document.get("universe_ref")))
    findings.extend(_check_business_lineage(path, document))
    return findings


def _check_removed_root_fields(path: Path, document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in sorted(_REMOVED_ROOT_FIELDS):
        if field in document:
            findings.append(
                _finding(
                    path,
                    "candidates.removed-root-field",
                    f"{field} is no longer part of candidates output",
                    field,
                )
            )
    return findings


def _check_removed_hash_fields(path: Path, document: object) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for location, node in _walk_mappings(document, prefix=None):
        for field in ("content_" + "sha256", "row_" + "sha256"):
            if field in node:
                findings.append(
                    _finding(
                        path,
                        "candidates.removed-hash-field",
                        f"{field} is no longer allowed in candidates output",
                        f"{location}.{field}" if location else field,
                    )
                )
        for field in sorted(_REMOVED_REFERENCE_FIELDS):
            if field in node:
                findings.append(
                    _finding(
                        path,
                        "candidates.removed-reference-field",
                        f"{field} has been replaced by repository reference fields",
                        f"{location}.{field}" if location else field,
                    )
                )
    return findings


def _check_universe_ref(
    path: Path, document: Mapping[str, Any], value: object
) -> list[ValidationFinding]:
    if not isinstance(value, Mapping):
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe_ref must be a repository ref mapping",
                "universe_ref",
            )
        ]
    root = repo_root_for(path)
    ref = value.get("ref_path")
    error = repository_ref_error(ref, root=root)
    if error is not None:
        return [_finding(path, "candidates.universe-ref", error, "universe_ref.ref_path")]
    assert isinstance(ref, str)
    ref_path = resolve_repository_ref(root, ref)
    if not ref.startswith("records/_universe-snapshots/"):
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe_ref.ref_path must point under records/_universe-snapshots/",
                "universe_ref.ref_path",
            )
        ]
    if ref_path.suffix not in {".yaml", ".yml"}:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe_ref.ref_path must point to a YAML file",
                "universe_ref.ref_path",
            )
        ]
    if not ref_path.is_file():
        return [
            _finding(
                path,
                "candidates.universe-ref",
                f"referenced universe file does not exist: {ref}",
                "universe_ref.ref_path",
            )
        ]
    try:
        loaded: object = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                f"failed to parse universe_ref: {exc}",
                "universe_ref.ref_path",
            )
        ]
    if not isinstance(loaded, Mapping):
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe_ref must point to a YAML mapping",
                "universe_ref.ref_path",
            )
        ]
    expected_snapshot_id = _expected_universe_snapshot_id(document)
    if expected_snapshot_id is not None and loaded.get("snapshot_id") != expected_snapshot_id:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                f"universe snapshot_id must equal {expected_snapshot_id}",
                "universe_ref.ref_path",
            )
        ]
    expected_asof = _expected_universe_asof(document)
    if expected_asof is not None and str(loaded.get("as_of")) != expected_asof:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                f"universe as_of must equal candidates asof_date {expected_asof}",
                "universe_ref.ref_path",
            )
        ]
    universe_size = document.get("universe_size")
    if universe_size is not None and loaded.get("universe_size") != universe_size:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                f"universe_size must equal {universe_size}",
                "universe_ref.ref_path",
            )
        ]
    if _is_canonical_candidates_record(path) and isinstance(universe_size, int):
        findings = _check_universe_members(path, document, loaded, universe_size)
        if findings:
            return findings
    return []


def _check_universe_members(
    path: Path,
    document: Mapping[str, Any],
    universe: Mapping[str, Any],
    universe_size: int,
) -> list[ValidationFinding]:
    members = universe.get("members")
    members_recorded = universe.get("members_recorded")
    members_scope = universe.get("members_scope")
    if members_scope not in {None, "full_universe", "candidates"}:
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe members_scope must be full_universe or candidates",
                "universe_ref.ref_path.members_scope",
            )
        ]
    candidates = document.get("candidates")
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    expected_member_count = (
        universe_size if members_scope in {None, "full_universe"} else candidate_count
    )
    if (
        not isinstance(members, list)
        or members_recorded != len(members)
        or len(members) != expected_member_count
        or universe.get("universe_size") != universe_size
    ):
        return [
            _finding(
                path,
                "candidates.universe-ref",
                "universe members_recorded, members length, and universe_size are inconsistent",
                "universe_ref.ref_path",
            )
        ]
    member_by_ticker: dict[str, Mapping[str, Any]] = {}
    for index, member in enumerate(members):
        if not isinstance(member, Mapping) or not isinstance(member.get("ticker"), str):
            return [
                _finding(
                    path,
                    "candidates.universe-ref",
                    "universe members must be mappings with ticker",
                    f"universe_ref.ref_path.members[{index}]",
                )
            ]
        if member["ticker"] in member_by_ticker:
            return [
                _finding(
                    path,
                    "candidates.universe-ref",
                    f"universe members must have unique ticker values: {member['ticker']}",
                    f"universe_ref.ref_path.members[{index}].ticker",
                )
            ]
        member_by_ticker[member["ticker"]] = member
    if not isinstance(candidates, list):
        return []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        ticker = candidate.get("ticker")
        if not isinstance(ticker, str):
            continue
        member = member_by_ticker.get(ticker)
        if member is None:
            return [
                _finding(
                    path,
                    "candidates.universe-ref",
                    f"candidate ticker is missing from universe members: {ticker}",
                    f"candidates[{index}].ticker",
                )
            ]
        for field in ("sector_33", "market_cap_oku", "avg_turnover_oku"):
            if field in candidate and member.get(field) != candidate.get(field):
                return [
                    _finding(
                        path,
                        "candidates.universe-ref",
                        f"universe member {field} must match candidate row for {ticker}",
                        f"candidates[{index}].{field}",
                    )
                ]
    return []


def _is_canonical_candidates_record(path: Path) -> bool:
    parts = path.parts
    return "records" in parts and "04-candidates" in parts


def _expected_universe_asof(document: Mapping[str, Any]) -> str | None:
    asof = document.get("asof_date")
    return asof if isinstance(asof, str) and asof else None


def _expected_universe_snapshot_id(document: Mapping[str, Any]) -> str | None:
    asof = _expected_universe_asof(document)
    if asof is None:
        return None
    return "universe-" + asof.replace("-", "")


def _walk_mappings(
    value: object, *, prefix: str | None
) -> Iterable[tuple[str, Mapping[str, object]]]:
    if isinstance(value, Mapping):
        location = prefix or ""
        yield location, value
        for key, child in value.items():
            child_prefix = f"{location}.{key}" if location else str(key)
            yield from _walk_mappings(child, prefix=child_prefix)
    elif isinstance(value, list):
        location = prefix or ""
        for index, child in enumerate(value):
            yield from _walk_mappings(child, prefix=f"{location}[{index}]")


def _check_business_lineage(
    path: Path,
    document: Mapping[str, Any],
) -> list[ValidationFinding]:
    run_id = document.get("run_id")
    asof_date = document.get("asof_date")
    candidates = document.get("candidates")
    if not isinstance(run_id, str) or not isinstance(candidates, list):
        return []
    findings: list[ValidationFinding] = []
    seen_candidate_ids: set[str] = set()
    seen_candidate_keys: set[str] = set()
    seen_evidence_hit_ids: set[str] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        ticker = candidate.get("ticker")
        if isinstance(candidate.get("screen_run_id"), str) and candidate["screen_run_id"] != run_id:
            findings.append(
                _finding(
                    path,
                    "candidates.screen-run-id",
                    "candidate.screen_run_id must equal root run_id",
                    f"candidates[{candidate_index}].screen_run_id",
                )
            )
        expected_key = f"{run_id}:{ticker}" if isinstance(ticker, str) else None
        if (
            isinstance(candidate.get("candidate_key"), str)
            and candidate["candidate_key"] != expected_key
        ):
            findings.append(
                _finding(
                    path,
                    "candidates.candidate-key",
                    "candidate_key must equal <run_id>:<ticker>",
                    f"candidates[{candidate_index}].candidate_key",
                )
            )
        candidate_id = candidate.get("candidate_id")
        expected_candidate_id = (
            f"candidate-{asof_date}-{ticker}"
            if isinstance(asof_date, str) and isinstance(ticker, str)
            else None
        )
        if isinstance(candidate_id, str):
            if candidate_id != expected_candidate_id:
                findings.append(
                    _finding(
                        path,
                        "candidates.candidate-id",
                        "candidate_id must equal candidate-<asof_date>-<ticker>",
                        f"candidates[{candidate_index}].candidate_id",
                    )
                )
            if candidate_id in seen_candidate_ids:
                findings.append(
                    _finding(
                        path,
                        "candidates.duplicate-candidate-id",
                        "candidate_id must be unique within the screen run",
                        f"candidates[{candidate_index}].candidate_id",
                    )
                )
            seen_candidate_ids.add(candidate_id)
        candidate_key = candidate.get("candidate_key")
        if isinstance(candidate_key, str):
            if candidate_key in seen_candidate_keys:
                findings.append(
                    _finding(
                        path,
                        "candidates.duplicate-candidate-key",
                        "candidate_key must be unique within the screen run",
                        f"candidates[{candidate_index}].candidate_key",
                    )
                )
            seen_candidate_keys.add(candidate_key)
        hits = candidate.get("evidence_hits")
        if not isinstance(hits, list):
            continue
        for hit_index, hit in enumerate(hits):
            if not isinstance(hit, Mapping):
                continue
            hit_id = hit.get("evidence_hit_id")
            if isinstance(hit_id, str):
                if hit_id in seen_evidence_hit_ids:
                    findings.append(
                        _finding(
                            path,
                            "candidates.duplicate-evidence-hit-id",
                            "evidence_hit_id must be unique within the screen run",
                            f"candidates[{candidate_index}].evidence_hits[{hit_index}].evidence_hit_id",
                        )
                    )
                seen_evidence_hit_ids.add(hit_id)
            source_status = hit.get("source_status")
            if source_status != "ok" and hit.get("sizing_eligible") is True:
                findings.append(
                    _finding(
                        path,
                        "candidates.ineligible-source-status",
                        "non-ok evidence source_status must not be sizing_eligible",
                        f"candidates[{candidate_index}].evidence_hits[{hit_index}].sizing_eligible",
                    )
                )
    return findings


def discover_candidates_files(root: Path) -> list[Path]:
    """Return all records/04-candidates/*.yaml files under ``root`` in sorted order."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _format_path(parts: Iterable[object]) -> str:
    parts_list = list(parts)
    if not parts_list:
        return ""
    rendered: list[str] = []
    for part in parts_list:
        if isinstance(part, int):
            rendered.append(f"[{part}]")
        else:
            rendered.append(f".{part}" if rendered else str(part))
    return "".join(rendered)


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
