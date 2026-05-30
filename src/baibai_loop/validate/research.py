"""Validate investment memo front matter and playbook body sections."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.policy_config import PORTFOLIO_POLICY

from .domain import (
    as_list,
    as_mapping,
    load_reference_mapping,
    number,
    repo_root_for,
    repository_ref_error,
    resolve_repository_ref,
)
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
_KNOWN_OUTCOMES = {"approved", "deferred", "rejected"}
_KNOWN_POSTURES = {"act_now", "wait_for_event", "wait_for_capital", "dropped"}
_KNOWN_MACRO_CONTEXT_EFFECTS = {"proceed", "caution", "defer"}
_KNOWN_MACRO_CONTEXT_FRESHNESS = {"current", "stale", "future"}
_KNOWN_MACRO_CONTEXT_FITS = {"tailwind", "neutral", "mixed", "headwind", "not_matched"}
_KNOWN_ENTRY_PREFLIGHT_ACTIONS = {"proceed", "starter", "defer", "exception"}
_KNOWN_ENTRY_PREFLIGHT_EXCEPTION_BASES = {
    "near_term_catalyst",
    "low_sizing",
    "low_correlation",
}
_ENTRY_PREFLIGHT_EFFECTIVE_DATE = date(2026, 6, 1)
_CANONICAL_SIZING_FIELDS = {
    "paper_proxy_position_size_yen",
    "real_order_intent_yen",
    "adv_participation_pct",
}
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(path: Path) -> object:
    stat = path.stat()
    return _load_yaml_cached(path.resolve().as_posix(), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _load_yaml_cached(path: str, mtime_ns: int, size: int) -> object:
    del mtime_ns, size
    # _YAML_LOADER is CSafeLoader or SafeLoader; keep yaml.load for the C loader path.
    return yaml.load(  # nosec B506
        Path(path).read_text(encoding="utf-8"), Loader=_YAML_LOADER
    )


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
    findings.extend(_check_candidate_lineage(path, front_matter))
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
    return Path(__file__).resolve().parents[3] / "records" / "_playbooks"


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


def _check_macro_context_fit(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    fit = front_matter.get("macro_context_fit")
    if fit is None:
        return []
    findings: list[ValidationFinding] = []
    if not isinstance(fit, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-fit",
                message="macro_context_fit must be a mapping",
                location="macro_context_fit",
            )
        ]
    freshness = fit.get("context_freshness")
    if freshness not in _KNOWN_MACRO_CONTEXT_FRESHNESS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-freshness",
                message="macro_context_fit.context_freshness must be current, stale, or future",
                location="macro_context_fit.context_freshness",
            )
        )
    context_fit = fit.get("fit")
    if context_fit not in _KNOWN_MACRO_CONTEXT_FITS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-fit-value",
                message=(
                    "macro_context_fit.fit must be tailwind, neutral, mixed, "
                    "headwind, or not_matched"
                ),
                location="macro_context_fit.fit",
            )
        )
    effect = fit.get("decision_effect")
    if effect not in _KNOWN_MACRO_CONTEXT_EFFECTS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-effect",
                message="macro_context_fit.decision_effect must be proceed, caution, or defer",
                location="macro_context_fit.decision_effect",
            )
        )
    decision = front_matter.get("research_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome == "approved" and effect == "defer":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-defer-approved",
                message="approved research cannot use macro_context_fit.decision_effect: defer",
                location="macro_context_fit.decision_effect",
            )
        )
    if outcome == "approved" and freshness == "future":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-future-approved",
                message="approved research cannot use a future macro context",
                location="macro_context_fit.context_freshness",
            )
        )
    ref = front_matter.get("macro_context_ref")
    if not isinstance(ref, str) or not ref:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-ref-required",
                message="macro_context_fit requires macro_context_ref",
                location="macro_context_ref",
            )
        )
    else:
        source_path = _resolve_record_ref(
            path, ref, prefixes=("records/01-macro-context/",), suffixes=(".yaml", ".yml")
        )
        if source_path is None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.macro-context-ref-missing",
                    message=f"macro_context_ref does not exist: {ref}",
                    location="macro_context_ref",
                )
            )
        else:
            findings.extend(
                _check_macro_context_dates(
                    path,
                    source_path=source_path,
                    front_matter=front_matter,
                    context_freshness=freshness if isinstance(freshness, str) else None,
                    context_fit=context_fit if isinstance(context_fit, str) else None,
                    decision_effect=effect if isinstance(effect, str) else None,
                    outcome=outcome if isinstance(outcome, str) else None,
                )
            )
    return findings


def _check_entry_preflight(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    decision = front_matter.get("research_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome != "approved":
        return []
    research_date = _research_record_date(front_matter, path=path)
    if research_date is None or research_date < _ENTRY_PREFLIGHT_EFFECTIVE_DATE:
        return []

    preflight = front_matter.get("entry_preflight")
    if not isinstance(preflight, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-required",
                message=("approved research dated 2026-06-01 or later requires entry_preflight"),
                location="entry_preflight",
            )
        ]

    findings: list[ValidationFinding] = []
    action = preflight.get("action")
    if action not in _KNOWN_ENTRY_PREFLIGHT_ACTIONS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-action",
                message=("entry_preflight.action must be proceed, starter, defer, or exception"),
                location="entry_preflight.action",
            )
        )
    reason = preflight.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-reason",
                message="entry_preflight.reason is required",
                location="entry_preflight.reason",
            )
        )

    preflight_freshness = preflight.get("macro_freshness")
    macro_fit = as_mapping(front_matter.get("macro_context_fit"))
    macro_freshness = macro_fit.get("context_freshness")
    if preflight_freshness not in _KNOWN_MACRO_CONTEXT_FRESHNESS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-macro-freshness",
                message="entry_preflight.macro_freshness must be current, stale, or future",
                location="entry_preflight.macro_freshness",
            )
        )
    elif isinstance(macro_freshness, str) and preflight_freshness != macro_freshness:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-macro-freshness-match",
                message=(
                    "entry_preflight.macro_freshness must match macro_context_fit.context_freshness"
                ),
                location="entry_preflight.macro_freshness",
            )
        )

    market_relative = number(preflight.get("market_relative_return_pct"))
    sector_relative = number(preflight.get("sector_or_peer_relative_return_pct"))
    exposure = as_mapping(preflight.get("tactical_exposure_after_order"))
    sector_exposure = number(exposure.get("sector_33_pct"))
    playbook_exposure = number(exposure.get("playbook_pct"))

    hard_triggers: list[str] = []
    if preflight_freshness == "future":
        hard_triggers.append("future macro freshness")
    if preflight_freshness == "stale":
        hard_triggers.append("stale macro freshness")
    if market_relative is not None and market_relative <= -3:
        hard_triggers.append("market relative return <= -3pt")
    if sector_relative is not None and sector_relative <= -3:
        hard_triggers.append("sector/peer relative return <= -3pt")
    if sector_exposure is not None and sector_exposure > 50:
        hard_triggers.append("sector exposure > 50% tactical budget")
    if playbook_exposure is not None and playbook_exposure > 50:
        hard_triggers.append("playbook exposure > 50% tactical budget")

    if preflight_freshness == "future":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-future-approved",
                message="approved research cannot use future macro freshness in entry_preflight",
                location="entry_preflight.macro_freshness",
            )
        )
    if action == "proceed" and hard_triggers:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-proceed-trigger",
                message=(
                    "entry_preflight.action: proceed is not allowed with "
                    + ", ".join(hard_triggers)
                ),
                location="entry_preflight.action",
            )
        )
    if action == "exception":
        basis = as_list(preflight.get("exception_basis"))
        known_basis = [item for item in basis if item in _KNOWN_ENTRY_PREFLIGHT_EXCEPTION_BASES]
        if len(known_basis) != len(basis) or not known_basis:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.action: exception requires exception_basis "
                        "from near_term_catalyst, low_sizing, or low_correlation"
                    ),
                    location="entry_preflight.exception_basis",
                )
            )
        if "near_term_catalyst" in known_basis and preflight.get("near_term_catalyst") is not True:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.exception_basis: near_term_catalyst requires "
                        "near_term_catalyst: true"
                    ),
                    location="entry_preflight.near_term_catalyst",
                )
            )
        if "low_sizing" in known_basis and (
            sector_exposure is None
            or playbook_exposure is None
            or sector_exposure > 25
            or playbook_exposure > 25
        ):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.exception_basis: low_sizing requires sector "
                        "and playbook exposure after order <= 25%"
                    ),
                    location="entry_preflight.exception_basis",
                )
            )
    return findings


def _check_macro_context_dates(
    path: Path,
    *,
    source_path: Path,
    front_matter: Mapping[str, object],
    context_freshness: str | None,
    context_fit: str | None,
    decision_effect: str | None,
    outcome: str | None,
) -> list[ValidationFinding]:
    try:
        raw = _load_yaml(source_path)
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-ref-parse",
                message=f"macro_context_ref cannot be parsed: {exc}",
                location="macro_context_ref",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-ref-parse",
                message="macro_context_ref must point to a mapping YAML",
                location="macro_context_ref",
            )
        ]
    as_of = _parse_date_value(raw.get("as_of"))
    valid_until = _parse_date_value(raw.get("valid_until"))
    research_date = _research_record_date(front_matter)
    if research_date is None:
        return []
    findings: list[ValidationFinding] = []
    if as_of is not None and as_of > research_date:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-ref-future",
                message="macro_context_ref.as_of must not be after the research record date",
                location="macro_context_ref",
            )
        )
    if context_freshness == "current" and (
        (as_of is not None and as_of > research_date)
        or (valid_until is not None and valid_until < research_date)
    ):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-current-window",
                message=(
                    "macro_context_fit.context_freshness: current requires research date "
                    "within macro context window"
                ),
                location="macro_context_fit.context_freshness",
            )
        )
    findings.extend(
        _check_macro_context_sector_fit(
            path,
            raw,
            front_matter=front_matter,
            context_fit=context_fit,
            decision_effect=decision_effect,
            outcome=outcome,
        )
    )
    return findings


def _check_macro_context_sector_fit(
    path: Path,
    macro_context: Mapping[object, object],
    *,
    front_matter: Mapping[str, object],
    context_fit: str | None,
    decision_effect: str | None,
    outcome: str | None,
) -> list[ValidationFinding]:
    sector = front_matter.get("sector_33")
    if not isinstance(sector, str) or not sector:
        return []
    expected_fit = _sector_fit_from_macro_context(macro_context, sector)
    if expected_fit is None:
        expected_fit = "not_matched"
    findings: list[ValidationFinding] = []
    if context_fit is not None and context_fit != expected_fit:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-sector-fit",
                message=(
                    "macro_context_fit.fit must match macro_context_ref sector_tilts "
                    f"for sector_33={sector}: expected {expected_fit}, got {context_fit}"
                ),
                location="macro_context_fit.fit",
            )
        )
    fit_payload = as_mapping(front_matter.get("macro_context_fit"))
    sizing_caution = as_list(fit_payload.get("sizing_caution"))
    if (
        outcome == "approved"
        and expected_fit == "headwind"
        and decision_effect == "proceed"
        and not sizing_caution
    ):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.macro-context-headwind-proceed",
                message=(
                    "approved research with a macro headwind cannot proceed without "
                    "macro_context_fit.sizing_caution"
                ),
                location="macro_context_fit.sizing_caution",
            )
        )
    return findings


def _sector_fit_from_macro_context(
    macro_context: Mapping[object, object], sector: str
) -> str | None:
    sector_tilts = macro_context.get("sector_tilts")
    if not isinstance(sector_tilts, Mapping):
        return None
    items = sector_tilts.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("scope") != "sector_33" or item.get("key") != sector:
            continue
        stance = item.get("stance")
        if isinstance(stance, str) and stance in _KNOWN_MACRO_CONTEXT_FITS:
            return stance
    return None


def _research_record_date(
    front_matter: Mapping[str, object], *, path: Path | None = None
) -> date | None:
    record_dt = _parse_datetime(front_matter.get("published_at")) or _parse_datetime(
        front_matter.get("recorded_at")
    )
    if record_dt is not None:
        return record_dt.date()
    decision = front_matter.get("research_decision")
    if isinstance(decision, Mapping):
        decision_dt = _parse_datetime(decision.get("decided_at"))
        if decision_dt is not None:
            return decision_dt.date()
    if path is not None:
        try:
            return date.fromisoformat(path.name[:10])
        except ValueError:
            return None
    return None


def _parse_date_value(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


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
                message=f"playbook_id must reference a known playbook family (got {playbook_id!r})",
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
    if outcome == "deferred" and posture not in {"wait_for_event", "wait_for_capital"}:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.deferred-posture",
                message="deferred research decisions must wait for event or capital",
                location="research_decision.posture",
            )
        )
    if outcome == "deferred" and "deferral_reason" not in decision:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.deferral-reason-required",
                message="deferred research decisions require deferral_reason",
                location="research_decision.deferral_reason",
            )
        )
    if outcome == "rejected" and posture != "dropped":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.rejected-posture",
                message="rejected research decisions must use posture: dropped",
                location="research_decision.posture",
            )
        )
    return findings


def _check_candidate_lineage(
    path: Path,
    front_matter: Mapping[str, object],
) -> list[ValidationFinding]:
    if not _is_repository_research_record(path):
        return []
    findings: list[ValidationFinding] = []
    candidate_ref = as_mapping(front_matter.get("candidate_ref"))
    candidates_ref = candidate_ref.get("candidates_ref")
    if not isinstance(candidates_ref, str) or not candidates_ref:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-required",
                message="research records require candidate_ref.candidates_ref",
                location="candidate_ref.candidates_ref",
            )
        ]
    candidate_path = _resolve_candidate_ref(path, candidates_ref)
    if candidate_path is None:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-missing",
                message=f"candidate_ref.candidates_ref does not exist: {candidates_ref}",
                location="candidate_ref.candidates_ref",
            )
        ]
    try:
        document = _load_yaml(candidate_path)
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-parse",
                message=f"failed to read candidate_ref.candidates_ref: {exc}",
                location="candidate_ref.candidates_ref",
            )
        ]
    if not isinstance(document, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-parse",
                message="candidate_ref.candidates_ref must point to a candidates mapping",
                location="candidate_ref.candidates_ref",
            )
        ]
    ref_ticker = candidate_ref.get("ticker")
    if not isinstance(ref_ticker, str) or not ref_ticker:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-ticker",
                message="candidate_ref.ticker is required",
                location="candidate_ref.ticker",
            )
        )
    elif ref_ticker != front_matter.get("ticker"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-ticker",
                message="candidate_ref.ticker must match research ticker",
                location="candidate_ref.ticker",
            )
        )
    candidate_row = _candidate_row_for_front(front_matter, document)
    if candidate_row is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidate-ref-match",
                message="candidate_ref must match a candidate row by ticker",
                location="candidate_ref",
            )
        )
    else:
        findings.extend(_check_copied_candidate_fields(path, front_matter, candidate_row))
    return findings


def _candidate_row_for_front(
    front_matter: Mapping[str, object],
    document: Mapping[str, object],
) -> Mapping[str, object] | None:
    candidate_ref = as_mapping(front_matter.get("candidate_ref"))
    ticker = candidate_ref.get("ticker")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not isinstance(ticker, str):
        return None
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate.get("ticker") == ticker:
            return candidate
    return None


def _check_copied_candidate_fields(
    path: Path,
    front_matter: Mapping[str, object],
    candidate: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in ("avg_turnover_oku", "market_cap_oku", "sector_33"):
        actual = front_matter.get(field)
        expected = candidate.get(field)
        if expected is not None and actual != expected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.candidate-field-copy",
                    message=f"{field} must match candidate_ref source row",
                    location=field,
                )
            )
    valuation = as_mapping(front_matter.get("valuation"))
    metrics = as_mapping(candidate.get("metrics"))
    for field, metric_id in (
        ("p_s", "p_s"),
        ("ocf_yield", "ocf_yield"),
        ("fcf_yield", "fcf_yield"),
        ("net_cash_to_market_cap", "net_cash_to_market_cap"),
    ):
        if metric_id not in metrics:
            continue
        expected = metrics.get(metric_id)
        actual = valuation.get(field)
        if actual != expected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.candidate-field-copy",
                    message=f"valuation.{field} must match candidate_ref metrics.{metric_id}",
                    location=f"valuation.{field}",
                )
            )
    return findings


def _is_repository_research_record(path: Path) -> bool:
    root = repo_root_for(path)
    try:
        path.resolve().relative_to((root / "records/05-research").resolve())
    except ValueError:
        return False
    return True


def _resolve_record_ref(
    path: Path,
    ref: str,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> Path | None:
    root = repo_root_for(path)
    if repository_ref_error(ref, root=root) is not None:
        return None
    candidate = resolve_repository_ref(root, ref)
    if not ref.startswith(prefixes) or candidate.suffix not in suffixes:
        return None
    return candidate if candidate.is_file() else None


def _resolve_candidate_ref(path: Path, ref: str) -> Path | None:
    root = repo_root_for(path)
    if repository_ref_error(ref, root=root) is not None:
        return None
    candidate = resolve_repository_ref(root, ref)
    if not ref.startswith("records/04-candidates/") or candidate.suffix not in {
        ".yaml",
        ".yml",
    }:
        return None
    return candidate if candidate.is_file() else None


def _check_sizing_invariants(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    raw_sizing = front_matter.get("position_sizing_overlay")
    decision = as_mapping(front_matter.get("research_decision"))
    if not isinstance(raw_sizing, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.position-sizing-shape",
                message="position_sizing_overlay must be a mapping with canonical sizing fields",
                location="position_sizing_overlay",
            )
        ]
    sizing = raw_sizing
    findings = _check_position_sizing_overlay_shape(path, front_matter, sizing)
    if decision.get("outcome") != "approved":
        expected_zero = {
            "paper_proxy_position_size_yen": 0,
            "real_order_intent_yen": 0,
            "adv_participation_pct": 0,
        }
        for field, expected_value in expected_zero.items():
            value = sizing.get(field)
            tolerance = 1 if field.endswith("_yen") else 0.0001
            if not _close(value, expected_value, tolerance=tolerance):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="research.rejected-sizing",
                        message=f"{field} must be zero for non-approved decisions",
                        location=f"position_sizing_overlay.{field}",
                    )
                )
        return findings

    avg_turnover_oku = number(front_matter.get("avg_turnover_oku"))
    if avg_turnover_oku is None or avg_turnover_oku <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.adv-participation-input",
                message="avg_turnover_oku is required to derive adv_participation_pct",
                location="avg_turnover_oku",
            )
        )
        return findings

    return findings + _check_approved_position_sizing_limits(path, front_matter, sizing)


def _check_position_sizing_overlay_shape(
    path: Path,
    front_matter: Mapping[str, object],
    sizing: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in sorted(_CANONICAL_SIZING_FIELDS):
        if field not in sizing:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.position-sizing-missing-field",
                    message=f"position_sizing_overlay.{field} is required",
                    location=f"position_sizing_overlay.{field}",
                )
            )
    return findings


def _check_approved_position_sizing_limits(
    path: Path,
    front_matter: Mapping[str, object],
    sizing: Mapping[str, object],
    policy: Mapping[str, Any] = PORTFOLIO_POLICY,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    capital = as_mapping(policy.get("capital_basis"))
    risk = as_mapping(policy.get("risk_budget"))
    scaling = as_mapping(policy.get("execution_scaling"))
    avg_turnover_oku = number(front_matter.get("avg_turnover_oku"))
    paper_yen = number(sizing.get("paper_proxy_position_size_yen"))
    real_intent = number(sizing.get("real_order_intent_yen"))
    adv_participation_pct = number(sizing.get("adv_participation_pct"))

    numeric_fields = {
        "paper_proxy_position_size_yen": paper_yen,
        "real_order_intent_yen": real_intent,
        "adv_participation_pct": adv_participation_pct,
    }
    for field, value in numeric_fields.items():
        if value is None or value < 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.position-sizing-value",
                    message=f"position_sizing_overlay.{field} must be a non-negative number",
                    location=f"position_sizing_overlay.{field}",
                )
            )
    if findings:
        return findings
    assert paper_yen is not None
    assert real_intent is not None
    assert adv_participation_pct is not None
    assert avg_turnover_oku is not None

    if paper_yen <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.paper-proxy-position-size",
                message="approved research requires positive paper_proxy_position_size_yen",
                location="position_sizing_overlay.paper_proxy_position_size_yen",
            )
        )

    expected_adv = round(
        paper_yen / (avg_turnover_oku * 100_000_000) * 100 if avg_turnover_oku > 0 else 0.0,
        4,
    )
    if not _close(adv_participation_pct, expected_adv, tolerance=0.0001):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.adv-participation-pct",
                message=(
                    "adv_participation_pct must derive from "
                    "paper_proxy_position_size_yen and avg_turnover_oku"
                ),
                location="position_sizing_overlay.adv_participation_pct",
            )
        )

    scaled_real = paper_yen * ((number(scaling.get("paper_to_real_order_notional_pct")) or 0) / 100)

    paper_cap = number(risk.get("max_paper_proxy_position_size_yen"))
    if paper_cap is not None and paper_yen > paper_cap:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.paper-proxy-position-cap",
                message=f"paper_proxy_position_size_yen must not exceed policy cap {paper_cap:g}",
                location="position_sizing_overlay.paper_proxy_position_size_yen",
            )
        )

    adv_cap_pct = number(risk.get("max_adv_participation_pct"))
    liquidity_cap = (
        avg_turnover_oku * 100_000_000 * adv_cap_pct / 100 if adv_cap_pct is not None else None
    )
    real_caps = {
        "paper_to_real_order_notional_pct": scaled_real,
        "tactical_real_budget_yen": number(capital.get("tactical_real_budget_yen")),
        "max_real_order_notional_yen": number(risk.get("max_real_order_notional_yen")),
        "max_adv_participation_pct": liquidity_cap,
    }
    for cap_name, cap_value in real_caps.items():
        if cap_value is not None and real_intent > cap_value + 1:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.real-order-intent-cap",
                    message=f"real_order_intent_yen must not exceed {cap_name} cap {cap_value:g}",
                    location="position_sizing_overlay.real_order_intent_yen",
                )
            )
    return findings


def _check_corporate_action_check(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    decision = as_mapping(front_matter.get("research_decision"))
    if decision.get("outcome") != "approved":
        return []
    check = front_matter.get("corporate_action_check")
    if not isinstance(check, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.corporate-action-check",
                message="approved research requires corporate_action_check",
                location="corporate_action_check",
            )
        ]
    if check.get("checked") is not True:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.corporate-action-check",
                message="corporate_action_check.checked must be true for approved research",
                location="corporate_action_check.checked",
            )
        ]
    result = check.get("result")
    if result not in {"none", "found", "not_applicable"}:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.corporate-action-check-result",
                message="corporate_action_check.result must be none, found, or not_applicable",
                location="corporate_action_check.result",
            )
        ]
    if result != "none":
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.corporate-action-check-result",
                message="approved research requires corporate_action_check.result to be none",
                location="corporate_action_check.result",
            )
        ]
    return []


def _load_candidate_document(
    path: Path, front_matter: Mapping[str, object]
) -> Mapping[str, Any] | None:
    candidate_ref = front_matter.get("candidate_ref")
    ref_value: object = (
        candidate_ref.get("candidates_ref") if isinstance(candidate_ref, Mapping) else None
    )
    if not isinstance(ref_value, str) or not ref_value:
        return None
    candidate_path = _resolve_candidate_ref(path, ref_value)
    if candidate_path is None:
        return None
    try:
        loaded: object = _load_yaml(candidate_path)
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed


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
        expected_downside = round((1 - stop / entry) * 100, 2)
        risk_reward = round(expected_upside / expected_downside, 2) if expected_downside else None
        if not _close(payoff.get("expected_upside_pct"), expected_upside, tolerance=0.01):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.expected-upside",
                    message=f"expected_upside_pct must equal {expected_upside}",
                    location="thesis_payoff.expected_upside_pct",
                )
            )
        if not _close(payoff.get("expected_downside_pct"), expected_downside, tolerance=0.01):
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
            payoff.get("risk_reward_ratio"), risk_reward, tolerance=0.01
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


def _check_reference_refs(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    specs = {
        "playbook_ref": (("records/_playbooks/",), (".md",)),
    }
    for field, (prefixes, suffixes) in specs.items():
        value = front_matter.get(field)
        findings.extend(
            _check_repository_ref(
                path,
                value,
                location=field,
                code="research.reference-ref",
                prefixes=prefixes,
                suffixes=suffixes,
            )
        )
    return findings


def _check_repository_ref(
    path: Path,
    value: object,
    *,
    location: str,
    code: str,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> list[ValidationFinding]:
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location} must be a repository ref mapping",
                location=location,
            )
        ]
    findings: list[ValidationFinding] = []
    root = repo_root_for(path)
    ref = value.get("ref_path")
    error = repository_ref_error(ref, root=root)
    if error is not None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=error,
                location=f"{location}.ref_path",
            )
        )
        return findings
    assert isinstance(ref, str)
    ref_path = resolve_repository_ref(root, ref)
    if not ref_path.is_file():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file does not exist: {ref}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    if not ref.startswith(prefixes):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must point under {', '.join(prefixes)}",
                location=f"{location}.ref_path",
            )
        )
    if ref_path.suffix not in suffixes:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must use suffix {', '.join(suffixes)}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    try:
        if ref_path.suffix == ".md":
            load_reference_mapping(root, value)
        else:
            loaded = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("referenced YAML must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file cannot be parsed: {exc}",
                location=f"{location}.ref_path",
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
