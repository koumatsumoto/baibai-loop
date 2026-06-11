"""Macro-context fit, date-consistency, and sector-fit checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from baibai_loop.validate.domain import (
    as_list,
    as_mapping,
)
from baibai_loop.validate.errors import ValidationFinding

from .shared import (
    _KNOWN_MACRO_CONTEXT_FRESHNESS,
    _load_yaml,
    _parse_date_value,
    _research_record_date,
    _resolve_record_ref,
)

_KNOWN_MACRO_CONTEXT_EFFECTS = {"proceed", "caution", "defer"}
_KNOWN_MACRO_CONTEXT_FITS = {"tailwind", "neutral", "mixed", "headwind", "not_matched"}


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
