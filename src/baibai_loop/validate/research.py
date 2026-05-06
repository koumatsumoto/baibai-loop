"""Validate investment memo front matter and playbook body sections."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding
from .playbook_schema import (
    PlaybookSchemaError,
    discover_playbook_schemas,
    load_playbook_schema,
    validate_research_body,
)

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "research.json"

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
_REMOVED_FRONT_MATTER_FIELDS: tuple[str, ...] = (
    "playbook",
    "decision",
    "_".join(("macro", "gate")),
    "_".join(("macro", "gate", "override")),
    "_".join(("position", "size", "oku")),
    "_".join(("hypothetical", "position", "size", "oku")),
    "_".join(("supporting", "sig" + "nals")),
    "_".join(("adv", "participation", "pct")),
)
_KNOWN_OUTCOMES = {"approved", "passed", "rejected"}
_KNOWN_POSTURES = {"act_now", "wait_for_event", "wait_for_capital", "dropped"}
_KNOWN_GATE_EFFECTS = {"pass", "conditional", "block"}


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


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
    findings.extend(_check_removed_fields(path, front_matter))
    findings.extend(_check_ticker(path, front_matter))
    findings.extend(_check_playbook(path, front_matter, known_playbooks))
    findings.extend(_check_decision(path, front_matter))
    findings.extend(_check_gate(path, front_matter))
    findings.extend(_check_evidence_and_counts(path, front_matter))
    findings.extend(_check_payoff(path, front_matter))
    findings.extend(_check_snapshot_refs(path, front_matter))

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
    return Path(__file__).resolve().parents[3] / "records" / "_playbooks"


def _validate_schema(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(front_matter):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"research.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def _check_removed_fields(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    return [
        ValidationFinding(
            severity="error",
            target=path,
            code="research.removed-field",
            message=f"removed front matter field is not allowed: {field}",
            location=field,
        )
        for field in _REMOVED_FRONT_MATTER_FIELDS
        if field in front_matter
    ]


def _check_ticker(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    ticker = front_matter.get("ticker")
    if not isinstance(ticker, str) or not _TICKER_PATTERN.match(ticker):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.ticker-format",
                message=f"ticker must be 4 alphanumeric uppercase chars (got {ticker!r})",
                location="ticker",
            )
        ]
    return []


def _check_playbook(
    path: Path,
    front_matter: Mapping[str, object],
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    playbook_id = front_matter.get("playbook_id")
    if not isinstance(playbook_id, str) or playbook_id not in known_playbooks:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-playbook",
                message=f"playbook_id must reference a known snapshot family (got {playbook_id!r})",
                location="playbook_id",
            )
        ]
    return []


def _check_decision(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    decision = front_matter.get("research_decision")
    if not isinstance(decision, Mapping):
        return []
    findings: list[ValidationFinding] = []
    outcome = decision.get("outcome")
    posture = decision.get("posture")
    if outcome not in _KNOWN_OUTCOMES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-outcome",
                message=f"research_decision.outcome must be one of {_KNOWN_OUTCOMES}",
                location="research_decision.outcome",
            )
        )
    if posture not in _KNOWN_POSTURES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-posture",
                message=f"research_decision.posture must be one of {_KNOWN_POSTURES}",
                location="research_decision.posture",
            )
        )
    if outcome == "approved" and posture != "act_now":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.approved-posture",
                message="approved research decisions must use posture: act_now",
                location="research_decision.posture",
            )
        )
    if outcome == "rejected" and "rejection_reason" not in decision:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.rejection-reason-required",
                message="rejected research decisions require rejection_reason",
                location="research_decision.rejection_reason",
            )
        )
    if outcome == "passed" and posture != "act_now" and "deferral_reason" not in decision:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.deferral-reason-required",
                message="deferred passed decisions require deferral_reason",
                location="research_decision.deferral_reason",
            )
        )
    return findings


def _check_gate(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    gate = front_matter.get("macro_regime_gate")
    if not isinstance(gate, Mapping):
        return []
    effect = gate.get("decision_effect")
    if effect not in _KNOWN_GATE_EFFECTS:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-gate-effect",
                message=f"macro_regime_gate.decision_effect must be one of {_KNOWN_GATE_EFFECTS}",
                location="macro_regime_gate.decision_effect",
            )
        ]
    decision = front_matter.get("research_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome == "approved" and effect == "block":
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.blocked-gate-approved",
                message="approved research cannot use macro_regime_gate.decision_effect: block",
                location="macro_regime_gate.decision_effect",
            )
        ]
    return []


def _check_evidence_and_counts(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    decisions = front_matter.get("candidate_evidence_decisions")
    selected = front_matter.get("selected_supporting_evidence_refs")
    research_hits = front_matter.get("research_evidence_hits", [])
    if not isinstance(decisions, list):
        return findings

    effective_components = _effective_independence_components(
        path,
        front_matter,
        decisions,
        research_hits if isinstance(research_hits, list) else [],
        findings,
    )
    independent_count = front_matter.get("independent_evidence_count")
    if isinstance(independent_count, int) and independent_count != len(effective_components):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.independent-evidence-count",
                message=(
                    "independent_evidence_count must equal research-time "
                    "effective sizing-eligible evidence decisions"
                ),
                location="independent_evidence_count",
            )
        )
    decision = front_matter.get("research_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome == "approved":
        if not isinstance(selected, list) or not selected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.supporting-evidence-required",
                    message="approved research requires selected_supporting_evidence_refs",
                    location="selected_supporting_evidence_refs",
                )
            )
        if isinstance(research_hits, list):
            has_risk_review = any(
                isinstance(hit, Mapping) and hit.get("evidence_polarity") in {"risk", "contradicts"}
                for hit in research_hits
            )
            if not has_risk_review:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="research.risk-evidence-required",
                        message=(
                            "approved research requires at least one risk or "
                            "contradicting evidence review"
                        ),
                        location="research_evidence_hits",
                    )
                )
    return findings


def _effective_independence_components(
    path: Path,
    front_matter: Mapping[str, object],
    decisions: Sequence[object],
    research_hits: Sequence[object],
    findings: list[ValidationFinding],
) -> set[str]:
    candidate_components = _load_candidate_components(path, front_matter)
    components: set[str] = set()
    for index, item in enumerate(decisions):
        if not isinstance(item, Mapping) or item.get("effective_sizing_eligible") is not True:
            continue
        hit_id = item.get("evidence_hit_id")
        if not isinstance(hit_id, str) or not hit_id:
            continue
        component = item.get("independence_component_id")
        if not isinstance(component, str) or not component:
            component = (
                candidate_components.get(hit_id) if candidate_components is not None else None
            )
        if isinstance(component, str) and component:
            components.add(component)
        elif candidate_components is not None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.independence-component",
                    message=(
                        "effective candidate evidence must resolve to an independence_component_id"
                    ),
                    location=f"candidate_evidence_decisions[{index}].evidence_hit_id",
                )
            )
        else:
            components.add(f"unresolved:{hit_id}")

    for index, hit in enumerate(research_hits):
        if not isinstance(hit, Mapping):
            continue
        if hit.get("decision_role") != "sizing_evidence" or hit.get("sizing_eligible") is not True:
            continue
        component = hit.get("independence_component_id")
        if isinstance(component, str) and component:
            components.add(component)
            continue
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.research-evidence-component",
                message="sizing-eligible research evidence requires independence_component_id",
                location=f"research_evidence_hits[{index}].independence_component_id",
            )
        )
    return components


def _load_candidate_components(
    path: Path,
    front_matter: Mapping[str, object],
) -> dict[str, str] | None:
    candidate_ref = front_matter.get("candidate_ref")
    ref_value: object = (
        candidate_ref.get("candidates_ref") if isinstance(candidate_ref, Mapping) else None
    )
    if not isinstance(ref_value, str):
        ref_value = front_matter.get("candidates_ref")
    if not isinstance(ref_value, str) or not ref_value:
        return None
    candidate_path = _resolve_record_ref(path, ref_value)
    if candidate_path is None:
        return None
    try:
        loaded: object = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, Mapping):
        return None
    components: dict[str, str] = {}
    candidates = loaded.get("candidates")
    if not isinstance(candidates, list):
        return components
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        hits = candidate.get("evidence_hits")
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, Mapping):
                continue
            hit_id = hit.get("evidence_hit_id")
            component = hit.get("independence_component_id")
            if isinstance(hit_id, str) and isinstance(component, str) and component:
                components[hit_id] = component
    return components


def _resolve_record_ref(path: Path, ref: str) -> Path | None:
    relative = Path(ref)
    if relative.is_absolute():
        return relative if relative.is_file() else None
    for parent in (path.parent, *path.parents):
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    return None


def _check_payoff(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    payoff = front_matter.get("thesis_payoff")
    if not isinstance(payoff, Mapping):
        return []
    entry = _number(payoff.get("max_entry_price_yen"))
    target = _number(payoff.get("target_price_yen"))
    stop = _number(payoff.get("stop_loss_yen"))
    findings: list[ValidationFinding] = []
    if entry is not None and target is not None and stop is not None:
        if not stop < entry < target:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.payoff-order",
                    message=(
                        "long-only payoff must satisfy stop_loss < max_entry_price < target_price"
                    ),
                    location="thesis_payoff",
                )
            )
        expected_upside = round((target / entry - 1) * 100, 2)
        expected_downside = round((entry / stop - 1) * 100, 2)
        risk_reward = round(expected_upside / expected_downside, 2) if expected_downside else None
        if not _close(payoff.get("expected_upside_pct"), expected_upside, tolerance=0.15):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.expected-upside",
                    message=f"expected_upside_pct must equal {expected_upside}",
                    location="thesis_payoff.expected_upside_pct",
                )
            )
        if not _close(payoff.get("expected_downside_pct"), expected_downside, tolerance=0.15):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.expected-downside",
                    message=f"expected_downside_pct must equal {expected_downside}",
                    location="thesis_payoff.expected_downside_pct",
                )
            )
        if risk_reward is not None and not _close(
            payoff.get("risk_reward_ratio"), risk_reward, tolerance=0.05
        ):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.risk-reward",
                    message=f"risk_reward_ratio must equal {risk_reward}",
                    location="thesis_payoff.risk_reward_ratio",
                )
            )
    return findings


def _check_snapshot_refs(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in ("playbook_snapshot", "policy_snapshot", "portfolio_exposure_snapshot_ref"):
        value = front_matter.get(field)
        if not isinstance(value, Mapping):
            continue
        ref = value.get("ref_path")
        digest = value.get("content_sha256")
        if not isinstance(ref, str) or not ref:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.snapshot-ref",
                    message=f"{field}.ref_path is required",
                    location=f"{field}.ref_path",
                )
            )
        if not isinstance(digest, str) or not re.match(r"^sha256:[0-9a-f]{64}$", digest):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.snapshot-hash",
                    message=f"{field}.content_sha256 must be sha256:<64 hex chars>",
                    location=f"{field}.content_sha256",
                )
            )
    return findings


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _close(value: object, expected: float, *, tolerance: float) -> bool:
    number = _number(value)
    return number is not None and abs(number - expected) <= tolerance


def _format_path(parts: Iterable[object]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
