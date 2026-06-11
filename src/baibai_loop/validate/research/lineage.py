"""Candidate-lineage checks: source candidate reference and copied fields."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from baibai_loop.validate.domain import (
    as_mapping,
)
from baibai_loop.validate.errors import ValidationFinding

from .shared import (
    _is_repository_research_record,
    _load_yaml,
    _resolve_candidate_ref,
)


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
                code="research.candidate-ref-path",
                message=(
                    "candidate_ref.candidates_ref must be a repo-relative YAML path "
                    f"under records/04-candidates/: {candidates_ref}"
                ),
                location="candidate_ref.candidates_ref",
            )
        ]
    if not candidate_path.is_file():
        # Candidates YAML is a local store (kept out of git); on a checkout
        # without the file the lineage facts cannot be cross-checked.
        return [
            ValidationFinding(
                severity="warning",
                target=path,
                code="research.candidate-ref-missing",
                message=(
                    "candidates file is not present on this checkout; lineage checks "
                    f"skipped (local store): {candidates_ref}"
                ),
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
