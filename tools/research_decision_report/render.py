# ruff: noqa: E501
"""Render detailed research, comparison, and a planning-only order as one HTML file.

The report is a human-facing projection, never a new source of truth. Narrative
findings come from ``findings.yaml``; valuation, scenarios, permanent-loss risks,
selection, and order numbers are joined from the opportunity workspace and the
``plan-limit`` output.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketError,
    FiveYearBaseBreakEvenResult,
    decision_packet_core_hash,
    evaluate_decision_packet,
    load_decision_packet,
)
from baibai_engine.research.execution_policy import ExecutionPolicyError, max_acceptable_price
from baibai_engine.research.opportunity import BOARD_LOT, PLANNING_TICK_SIZE_YEN

TRADINGVIEW = "https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A{ticker}"
_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")
_TICKER = r"^[0-9A-Z]{4}$"
_LEDGER_FALLBACK_WARNING_PREFIX = "portfolio_exposure_ledger_fallback:"


class ReportError(ValueError):
    """Raised when report inputs cannot be joined without inventing facts."""


def _as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("must be an ISO date string")
    return date.fromisoformat(value)


class Evidence(BaseModel):
    model_config = _CONFIG

    statement: Annotated[str, Field(min_length=1)]
    kind: Literal["observed", "derived", "estimate", "management_claim"]
    source_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("source_ids", mode="before")
    @classmethod
    def _sources(cls, value: object) -> object:
        return _as_tuple(value)


class Finding(BaseModel):
    model_config = _CONFIG

    heading: Annotated[str, Field(min_length=1)]
    conclusion: Annotated[str, Field(min_length=1)]
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1)]

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> object:
        return _as_tuple(value)


class Catalyst(BaseModel):
    model_config = _CONFIG

    expected_on: date | None
    event: Annotated[str, Field(min_length=1)]
    decision_impact: Annotated[str, Field(min_length=1)]

    @field_validator("expected_on", mode="before")
    @classmethod
    def _expected_on(cls, value: object) -> date | None:
        return None if value is None else _date(value)


class SourceMetadata(BaseModel):
    model_config = _CONFIG

    source_id: Annotated[str, Field(min_length=1)]
    document_title: Annotated[str, Field(min_length=1)]
    published_at: date | None
    status: Literal["ok", "missing", "stale", "failed", "blocked"]
    note: str | None = None

    @field_validator("published_at", mode="before")
    @classmethod
    def _published_at(cls, value: object) -> date | None:
        return None if value is None else _date(value)

    @model_validator(mode="after")
    def _failed_source_has_impact_note(self) -> SourceMetadata:
        if self.status != "ok" and not self.note:
            raise ValueError("non-ok source metadata requires a note with decision impact")
        return self


class CandidateFindings(BaseModel):
    model_config = _CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER)]
    confidence: Literal["low", "medium", "high"]
    assigned_questions: Annotated[tuple[Finding, ...], Field(min_length=1)]
    business_model: Evidence
    value_capture: Evidence
    growth_quality: Annotated[tuple[Finding, ...], Field(min_length=1)]
    financial_resilience: Evidence
    domain_findings: tuple[Finding, ...] = ()
    strongest_countercase: Evidence
    catalysts: tuple[Catalyst, ...]
    unknowns: Annotated[tuple[str, ...], Field(min_length=1)]
    monitoring: Annotated[tuple[str, ...], Field(min_length=1)]
    source_metadata: Annotated[tuple[SourceMetadata, ...], Field(min_length=1)]

    @field_validator(
        "assigned_questions",
        "growth_quality",
        "domain_findings",
        "catalysts",
        "unknowns",
        "monitoring",
        "source_metadata",
        mode="before",
    )
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _as_tuple(value)

    @model_validator(mode="after")
    def _unique_source_metadata(self) -> CandidateFindings:
        ids = [item.source_id for item in self.source_metadata]
        if len(ids) != len(set(ids)):
            raise ValueError("source_metadata source_id values must be unique")
        return self


class ReportMeta(BaseModel):
    model_config = _CONFIG

    title: Annotated[str, Field(min_length=1)]
    as_of: date
    target_session: date
    budget_yen: Annotated[int, Field(gt=0)]
    source_freshness: Annotated[str, Field(min_length=1)]
    warnings: tuple[str, ...] = ()

    @field_validator("as_of", "target_session", mode="before")
    @classmethod
    def _dates(cls, value: object) -> date:
        return _date(value)

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> object:
        return _as_tuple(value)


class DecisionContext(BaseModel):
    model_config = _CONFIG

    portfolio_fit: Annotated[str, Field(min_length=1)]
    human_action: Annotated[str, Field(min_length=1)]
    entry_timing: Annotated[str, Field(min_length=1)] | None = None


class FindingsDocument(BaseModel):
    model_config = _CONFIG

    schema_version: Literal[1]
    meta: ReportMeta
    decision_context: DecisionContext
    candidates: Annotated[tuple[CandidateFindings, ...], Field(min_length=1)]

    @field_validator("candidates", mode="before")
    @classmethod
    def _candidates(cls, value: object) -> object:
        return _as_tuple(value)

    @model_validator(mode="after")
    def _unique_tickers(self) -> FindingsDocument:
        tickers = [item.ticker for item in self.candidates]
        if len(tickers) != len(set(tickers)):
            raise ValueError("candidate tickers must be unique")
        return self


_REVIEW_CHECK_IDS = frozenset(
    {
        "source_freshness",
        "user_questions_answered",
        "primary_source_traceability",
        "fact_estimate_separation",
        "countercase_and_unknowns",
        "scenario_and_fv_consistency",
        "comparison_and_portfolio_fit",
        "purchase_method_binding",
    }
)


class PacketBinding(BaseModel):
    model_config = _CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    core_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReviewBindings(BaseModel):
    model_config = _CONFIG

    manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    findings_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    comparison_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    packets: Annotated[tuple[PacketBinding, ...], Field(min_length=1)]
    proposal_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    selected_ticker: Annotated[str, Field(pattern=_TICKER)] | None

    @field_validator("packets", mode="before")
    @classmethod
    def _packets(cls, value: object) -> object:
        return _as_tuple(value)


class ReviewCheck(BaseModel):
    model_config = _CONFIG

    check_id: Annotated[str, Field(min_length=1)]
    status: Literal["pending", "pass", "fail"]
    note: Annotated[str, Field(min_length=1)] | None = None


class ReviewFinding(BaseModel):
    model_config = _CONFIG

    severity: Literal["info", "warning", "error"]
    ticker: Annotated[str, Field(pattern=_TICKER)] | None
    section: Annotated[str, Field(min_length=1)]
    issue: Annotated[str, Field(min_length=1)]
    required_change: str | None = None


class ReportReview(BaseModel):
    model_config = _CONFIG

    schema_version: Literal[1]
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: Annotated[str, Field(min_length=1)]
    reviewer_run_id: Annotated[str, Field(min_length=1)]
    reviewed_at: datetime
    conclusion: Literal["pending", "pass", "changes_required"]
    reviewed_inputs: ReviewBindings
    checks: tuple[ReviewCheck, ...]
    findings: tuple[ReviewFinding, ...] = ()

    @field_validator("checks", "findings", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _reviewed_at(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        else:
            raise ValueError("reviewed_at must be an ISO datetime")
        if parsed.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone")
        return parsed

    @model_validator(mode="after")
    def _complete_review(self) -> ReportReview:
        check_ids = [item.check_id for item in self.checks]
        if len(check_ids) != len(set(check_ids)) or set(check_ids) != _REVIEW_CHECK_IDS:
            raise ValueError("review checks must contain each required check_id exactly once")
        if self.conclusion == "pass":
            if any(item.status != "pass" for item in self.checks):
                raise ValueError("a passing review requires every check to pass")
            if any(item.severity == "error" for item in self.findings):
                raise ValueError("a passing review cannot retain error findings")
        return self


def _load_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        value = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReportError(f"cannot load {label}: {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise ReportError(f"{label} must be a mapping: {path}")
    return value


def _verify_manifest_inputs(manifest: Mapping[str, object]) -> None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ReportError("workspace manifest is missing external input hashes")
    for name in ("selection_output", "ledger"):
        value = inputs.get(name)
        if not isinstance(value, Mapping):
            raise ReportError(f"workspace manifest is missing input hash: {name}")
        if name == "ledger":
            if value.get("entity_id") != "portfolio-ledger" or not isinstance(
                value.get("append_head"), int
            ):
                raise ReportError("workspace manifest ledger revision is invalid")
            continue
        path_value = value.get("path")
        expected = value.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise ReportError(f"workspace manifest input ref is invalid: {name}")
        path = Path(path_value)
        if not path.is_file():
            raise ReportError(f"workspace external input is missing: {name}")
        if _sha256(path) != expected:
            raise ReportError(f"workspace external input hash is stale: {name}")


def _load_findings(path: Path) -> FindingsDocument:
    raw = _load_mapping(path, label="findings")
    try:
        return FindingsDocument.model_validate(raw)
    except ValidationError as error:
        raise ReportError(f"findings are invalid: {error}") from error


def _load_review(path: Path) -> ReportReview:
    raw = _load_mapping(path, label="report review")
    try:
        return ReportReview.model_validate(raw)
    except ValidationError as error:
        raise ReportError(f"report review is invalid: {error}") from error


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: object, *, digits: int = 1, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return f"{value:,.{digits}f}{suffix}"


def _external_link(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith((".localhost", ".local")):
        return None
    try:
        legacy_ipv4 = socket.inet_ntoa(socket.inet_aton(normalized))
    except OSError:
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            if not address.is_global:
                return None
    else:
        if not ipaddress.ip_address(legacy_ipv4).is_global:
            return None
    return value


def _dict_rows(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ReportError(f"{label} must be a list of mappings")
    return list(value)


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ReportError(f"{label} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ReportError(f"{label} must be a finite number") from error
    if not parsed.is_finite():
        raise ReportError(f"{label} must be a finite number")
    return parsed


def _string_items(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReportError(f"{label} must be a list of strings")
    return value


def _workspace_path(workspace: Path, ticker: str, filename: str) -> Path:
    root = workspace.resolve()
    path = (root / ticker / filename).resolve()
    if not path.is_relative_to(root):
        raise ReportError(f"workspace path escapes root for ticker {ticker}")
    return path


def _load_packet(workspace: Path, ticker: str) -> tuple[Path, DecisionPacketDocument]:
    path = _workspace_path(workspace, ticker, "packet-draft.yaml")
    try:
        packet = load_decision_packet(path)
    except (OSError, DecisionPacketError) as error:
        raise ReportError(f"packet is not schema-valid for {ticker}: {error}") from error
    if packet.input_snapshot.ticker != ticker:
        raise ReportError(f"packet ticker mismatch for {ticker}")
    return path, packet


def _evidence_items(findings: CandidateFindings) -> tuple[Evidence, ...]:
    groups: list[Iterable[Evidence]] = [
        (findings.business_model,),
        (findings.value_capture,),
        (findings.financial_resilience,),
        (findings.strongest_countercase,),
    ]
    groups.extend(item.evidence for item in findings.assigned_questions)
    groups.extend(item.evidence for item in findings.growth_quality)
    groups.extend(item.evidence for item in findings.domain_findings)
    return tuple(item for group in groups for item in group)


def _source_ids(findings: CandidateFindings) -> set[str]:
    return {source_id for item in _evidence_items(findings) for source_id in item.source_ids}


def _validate_sources(findings: CandidateFindings, packet: DecisionPacketDocument) -> None:
    packet_ids = {source.source_id for source in packet.input_snapshot.sources}
    used_ids = _source_ids(findings)
    missing = sorted(used_ids - packet_ids)
    if missing:
        raise ReportError(f"{findings.ticker} findings reference unknown source_ids: {missing}")
    metadata_ids = {item.source_id for item in findings.source_metadata}
    missing_metadata = sorted(
        source_id
        for source_id in used_ids
        if source_id not in metadata_ids
        and next(
            source for source in packet.input_snapshot.sources if source.source_id == source_id
        ).source_tier
        != "local_data"
    )
    if missing_metadata:
        raise ReportError(
            f"{findings.ticker} external sources need source_metadata: {missing_metadata}"
        )
    unknown_metadata = sorted(metadata_ids - packet_ids)
    if unknown_metadata:
        raise ReportError(
            f"{findings.ticker} source_metadata references unknown source_ids: {unknown_metadata}"
        )
    metadata = {item.source_id: item for item in findings.source_metadata}
    unavailable = {"missing", "failed", "blocked"}
    for evidence in _evidence_items(findings):
        unavailable_ids = sorted(
            source_id
            for source_id in evidence.source_ids
            if source_id in metadata and metadata[source_id].status in unavailable
        )
        if evidence.kind == "observed" and unavailable_ids:
            raise ReportError(
                f"{findings.ticker} observed evidence uses unavailable sources: {unavailable_ids}"
            )


def _comparison_index(comparison: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = _dict_rows(comparison.get("candidates"), label="comparison candidates")
    return {str(row.get("ticker")): row for row in rows}


def _require_completed_comparison(row: Mapping[str, object], ticker: str) -> None:
    required = (
        "temporary_mispricing_hypothesis",
        "permanent_loss_conclusion",
        "five_year_base_cagr_pct",
        "fair_value_yen",
        "fv_gap_pct",
        "portfolio_marginal_value",
        "strongest_countercase",
        "disposition",
        "disposition_reason",
    )
    missing = [field for field in required if row.get(field) is None]
    if missing:
        raise ReportError(f"comparison row is incomplete for {ticker}: {missing}")
    if row.get("disposition") not in {"select", "defer"}:
        raise ReportError(f"comparison disposition is invalid for {ticker}")


def _validate_comparison_values(
    row: Mapping[str, object], packet: DecisionPacketDocument, ticker: str
) -> None:
    if packet.estimates is None:
        raise ReportError(f"packet estimates are missing for {ticker}")
    base = next(
        (
            scenario
            for scenario in packet.estimates.scenarios
            if scenario.horizon_years == 5 and scenario.name == "base"
        ),
        None,
    )
    if base is None:
        raise ReportError(f"packet has no 5y/base scenario for {ticker}")
    tolerance = Decimal("0.0001")
    comparison_cagr = _decimal(
        row.get("five_year_base_cagr_pct"), label=f"{ticker} comparison five_year_base_cagr_pct"
    )
    packet_cagr = Decimal(str(base.claimed_total_return_cagr_pct))
    if abs(comparison_cagr - packet_cagr) > tolerance:
        raise ReportError(f"comparison 5y/base CAGR does not match packet for {ticker}")
    comparison_fv = _decimal(row.get("fair_value_yen"), label=f"{ticker} comparison fair_value_yen")
    packet_fv = packet.estimates.current_fair_value_yen
    if abs(comparison_fv - packet_fv) > tolerance:
        raise ReportError(f"comparison fair value does not match packet for {ticker}")
    entry = packet.estimates.entry_price_basis_yen
    expected_gap = (packet_fv / entry - Decimal(1)) * 100
    comparison_gap = _decimal(row.get("fv_gap_pct"), label=f"{ticker} comparison fv_gap_pct")
    if abs(comparison_gap - expected_gap) > Decimal("0.005"):
        raise ReportError(f"comparison FV gap does not match packet for {ticker}")


def _validate_selection_dispositions(
    rows: Mapping[str, Mapping[str, object]], selected_ticker: str | None
) -> None:
    selected_rows = {ticker for ticker, row in rows.items() if row.get("disposition") == "select"}
    expected = {selected_ticker} if selected_ticker is not None else set()
    if selected_rows != expected:
        raise ReportError("comparison dispositions do not match selected_ticker")


def _exposure_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReportError(f"{label} must be a mapping")
    return value


def _validate_exposure_row(
    value: object,
    *,
    label: str,
    expected_key: str,
    expected_warning_pct: Decimal,
    total_capital_yen: Decimal,
    order_notional_yen: Decimal,
) -> bool:
    row = _exposure_mapping(value, label=label)
    expected_fields = {
        "key",
        "current_and_reserved_yen",
        "prospective_yen",
        "prospective_pct",
        "warning_pct",
    }
    if set(row) != expected_fields:
        raise ReportError(f"{label} fields do not match the portfolio exposure contract")
    if row.get("key") != expected_key:
        raise ReportError(f"{label} key does not match the selected packet")
    current = _decimal(
        row.get("current_and_reserved_yen"), label=f"{label} current_and_reserved_yen"
    )
    prospective = _decimal(row.get("prospective_yen"), label=f"{label} prospective_yen")
    prospective_pct = _decimal(row.get("prospective_pct"), label=f"{label} prospective_pct")
    warning_pct = _decimal(row.get("warning_pct"), label=f"{label} warning_pct")
    if current < 0 or prospective <= 0 or prospective_pct <= 0 or warning_pct <= 0:
        raise ReportError(f"{label} exposure values must be positive (current may be zero)")
    if any(value != value.to_integral_value() for value in (current, prospective)):
        raise ReportError(f"{label} yen values must be whole numbers")
    if warning_pct != expected_warning_pct:
        raise ReportError(f"{label} warning_pct does not match portfolio policy")
    if prospective != current + order_notional_yen:
        raise ReportError(
            f"{label} prospective_yen must equal current exposure plus order notional"
        )
    raw_pct = prospective / total_capital_yen * 100
    expected_pct = raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if prospective_pct != expected_pct:
        raise ReportError(f"{label} prospective_pct does not match portfolio exposure arithmetic")
    return raw_pct > warning_pct


def _validate_portfolio_exposure(
    proposal: Mapping[str, object],
    *,
    selected_ticker: str,
    packet: DecisionPacketDocument,
    order_notional_yen: Decimal,
    warnings: list[str],
) -> None:
    exposure = _exposure_mapping(
        proposal.get("portfolio_exposure"), label="proposal portfolio_exposure"
    )
    expected_fields = {
        "price_as_of",
        "price_basis",
        "holding_valuation_status",
        "total_capital_yen",
        "ledger_fallback_tickers",
        "common_factor_empty_tickers",
        "ticker",
        "sector",
        "common_factors",
    }
    if set(exposure) != expected_fields:
        raise ReportError("proposal portfolio_exposure fields do not match the contract")
    if exposure.get("price_as_of") != proposal.get("price_as_of"):
        raise ReportError("portfolio exposure price_as_of does not match proposal price_as_of")
    if exposure.get("price_basis") != "last_close_unadjusted":
        raise ReportError("portfolio exposure price_basis must be last_close_unadjusted")
    total_capital = _decimal(
        exposure.get("total_capital_yen"), label="portfolio exposure total_capital_yen"
    )
    if total_capital <= 0:
        raise ReportError("portfolio exposure total_capital_yen must be positive")
    if total_capital != total_capital.to_integral_value():
        raise ReportError("portfolio exposure total_capital_yen must be a whole number")

    risk_policy = PORTFOLIO_POLICY["risk_budget"]
    ticker_warning_pct = Decimal(str(risk_policy["max_ticker_concentration_pct"]))
    sector_warning_pct = Decimal(str(risk_policy["max_sector_concentration_pct"]))
    factor_warning_pct = Decimal(str(risk_policy["max_common_factor_concentration_pct"]))

    warning_expectations = {
        "prospective_ticker_concentration_exceeds_warning": _validate_exposure_row(
            exposure.get("ticker"),
            label="portfolio exposure ticker",
            expected_key=selected_ticker,
            expected_warning_pct=ticker_warning_pct,
            total_capital_yen=total_capital,
            order_notional_yen=order_notional_yen,
        ),
        "prospective_sector_concentration_exceeds_warning": _validate_exposure_row(
            exposure.get("sector"),
            label="portfolio exposure sector",
            expected_key=packet.input_snapshot.sector,
            expected_warning_pct=sector_warning_pct,
            total_capital_yen=total_capital,
            order_notional_yen=order_notional_yen,
        ),
    }
    factor_rows = _dict_rows(
        exposure.get("common_factors"), label="portfolio exposure common_factors"
    )
    factor_keys = tuple(str(row.get("key")) for row in factor_rows)
    if factor_keys != packet.input_snapshot.common_factors:
        raise ReportError("portfolio exposure common-factor keys do not match the selected packet")
    for factor, row in zip(packet.input_snapshot.common_factors, factor_rows, strict=True):
        warning_expectations[
            f"prospective_common_factor_concentration_exceeds_warning:{factor}"
        ] = _validate_exposure_row(
            row,
            label=f"portfolio exposure common factor {factor}",
            expected_key=factor,
            expected_warning_pct=factor_warning_pct,
            total_capital_yen=total_capital,
            order_notional_yen=order_notional_yen,
        )
    warning_set = set(warnings)
    factor_warning_prefix = "prospective_common_factor_concentration_exceeds_warning:"
    fixed_concentration_warnings = {
        "prospective_ticker_concentration_exceeds_warning",
        "prospective_sector_concentration_exceeds_warning",
    }
    actual_concentration_warnings = {
        code
        for code in warning_set
        if code in fixed_concentration_warnings or code.startswith(factor_warning_prefix)
    }
    expected_concentration_warnings = {
        code for code, exceeds_threshold in warning_expectations.items() if exceeds_threshold
    }
    if actual_concentration_warnings != expected_concentration_warnings:
        raise ReportError("portfolio exposure concentration warnings do not match thresholds")

    fallback_tickers = _string_items(
        exposure.get("ledger_fallback_tickers"),
        label="portfolio exposure ledger_fallback_tickers",
    )
    if len(fallback_tickers) != len(set(fallback_tickers)):
        raise ReportError("portfolio exposure ledger_fallback_tickers must be unique")
    if any(
        len(ticker) != 4 or not ticker.isalnum() or ticker.upper() != ticker
        for ticker in fallback_tickers
    ):
        raise ReportError("portfolio exposure ledger_fallback_tickers contain an invalid ticker")
    expected_fallback_warnings = {
        f"{_LEDGER_FALLBACK_WARNING_PREFIX}{ticker}" for ticker in fallback_tickers
    }
    actual_fallback_warnings = {
        code for code in warning_set if code.startswith(_LEDGER_FALLBACK_WARNING_PREFIX)
    }
    if actual_fallback_warnings != expected_fallback_warnings:
        raise ReportError("portfolio exposure fallback tickers and warnings do not match")
    expected_valuation_status = (
        "mixed_with_ledger_fallback" if fallback_tickers else "same_asof_raw_close"
    )
    if exposure.get("holding_valuation_status") != expected_valuation_status:
        raise ReportError(
            "portfolio exposure holding_valuation_status does not match ledger fallbacks"
        )

    empty_factor_tickers = _string_items(
        exposure.get("common_factor_empty_tickers"),
        label="portfolio exposure common_factor_empty_tickers",
    )
    if empty_factor_tickers != sorted(set(empty_factor_tickers)):
        raise ReportError(
            "portfolio exposure common_factor_empty_tickers must be sorted and unique"
        )
    if any(
        len(ticker) != 4 or not ticker.isalnum() or ticker.upper() != ticker
        for ticker in empty_factor_tickers
    ):
        raise ReportError(
            "portfolio exposure common_factor_empty_tickers contain an invalid ticker"
        )
    coverage_warning = "portfolio_exposure_common_factor_coverage_incomplete"
    if warnings.count(coverage_warning) != int(bool(empty_factor_tickers)):
        raise ReportError(
            "portfolio exposure common-factor coverage warning does not match empty tickers"
        )


def _validate_proposal(
    *,
    proposal_path: Path | None,
    selected_ticker: str | None,
    packet_path: Path | None,
    packet: DecisionPacketDocument | None,
    manifest: Mapping[str, object],
    report_as_of: date,
    report_budget_yen: int,
    target_session: date,
) -> Mapping[str, object] | None:
    if selected_ticker is None:
        if proposal_path is not None:
            raise ReportError("proposal is forbidden when selected_ticker is null")
        return None
    if proposal_path is None or packet_path is None or packet is None:
        raise ReportError("selected_ticker requires a plan-limit proposal and packet")
    proposal = _load_mapping(proposal_path, label="plan-limit proposal")
    if proposal.get("ticker") != selected_ticker:
        raise ReportError("proposal ticker does not match selected_ticker")
    if proposal.get("status") not in {"planned_limit", "defer"}:
        raise ReportError("proposal status must be planned_limit or defer")
    if proposal.get("price_basis") != "last_close_unadjusted":
        raise ReportError("proposal price_basis must be last_close_unadjusted")
    if proposal.get("decision_packet_sha256") != _sha256(packet_path):
        raise ReportError("proposal decision_packet_sha256 does not match selected packet")
    if proposal.get("decision_packet_core_sha256") != decision_packet_core_hash(packet):
        raise ReportError("proposal decision_packet_core_sha256 does not match selected packet")
    review_path = _workspace_path(packet_path.parent.parent, selected_ticker, "review-draft.yaml")
    if proposal.get("independent_review_sha256") != _sha256(review_path):
        raise ReportError("proposal independent_review_sha256 does not match selected review")
    inputs = manifest.get("inputs")
    ledger = inputs.get("ledger") if isinstance(inputs, Mapping) else None
    ledger_entity = ledger.get("entity_id") if isinstance(ledger, Mapping) else None
    ledger_head = ledger.get("append_head") if isinstance(ledger, Mapping) else None
    if (
        proposal.get("source_ledger_entity") != ledger_entity
        or proposal.get("source_ledger_append_head") != ledger_head
    ):
        raise ReportError("proposal ledger revision does not match workspace ledger")
    expires_at = proposal.get("expires_at")
    try:
        expires_at_value = datetime.fromisoformat(str(expires_at))
    except ValueError as error:
        raise ReportError("proposal expires_at is invalid") from error
    if expires_at_value.utcoffset() is None:
        raise ReportError("proposal expires_at must include a timezone")
    if expires_at_value.date() != target_session:
        raise ReportError("proposal expiry does not match target_session")
    warnings = _string_items(proposal.get("warnings"), label="proposal warnings")
    _string_items(proposal.get("portfolio_annotations"), label="proposal portfolio_annotations")
    defer_reasons = _string_items(proposal.get("defer_reasons"), label="proposal defer_reasons")
    status = proposal["status"]
    quantity = _decimal(proposal.get("quantity"), label="proposal quantity")
    notional = _decimal(proposal.get("notional_yen"), label="proposal notional_yen")
    board_lot = _decimal(proposal.get("board_lot"), label="proposal board_lot")
    if board_lot != BOARD_LOT:
        raise ReportError("proposal board_lot does not match portfolio policy")
    if packet.estimates is None:
        raise ReportError("selected packet estimates are missing")
    try:
        expected_max_price = max_acceptable_price(packet, tick_size_yen=PLANNING_TICK_SIZE_YEN)
    except ExecutionPolicyError as error:
        raise ReportError(f"cannot derive proposal max acceptable price: {error}") from error
    proposal_max_price = _decimal(
        proposal.get("max_acceptable_price_yen"), label="proposal max_acceptable_price_yen"
    )
    if proposal_max_price != expected_max_price:
        raise ReportError("proposal max acceptable price does not match selected packet")
    if status == "planned_limit":
        if proposal.get("price_as_of") != report_as_of.isoformat():
            raise ReportError("planned_limit price_as_of does not match report as_of")
        close_price = _decimal(proposal.get("close_yen"), label="proposal close_yen")
        limit_price = _decimal(proposal.get("limit_price_yen"), label="proposal limit_price_yen")
        budget_min = _decimal(proposal.get("budget_min_yen"), label="proposal budget_min_yen")
        budget_max = _decimal(proposal.get("budget_max_yen"), label="proposal budget_max_yen")
        if budget_max != report_budget_yen:
            raise ReportError("proposal budget_max_yen does not match the report budget")
        if budget_min <= 0 or budget_min > budget_max:
            raise ReportError("proposal budget range is invalid")
        if close_price != packet.estimates.entry_price_basis_yen:
            raise ReportError("proposal close does not match selected packet entry price")
        if limit_price != close_price:
            raise ReportError("planned_limit price must equal the raw close")
        if limit_price <= 0 or limit_price > expected_max_price:
            raise ReportError("planned_limit price must be positive and at or below max price")
        lot_notional = close_price * BOARD_LOT
        if lot_notional <= budget_max:
            expected_quantity = max(int(budget_max // lot_notional), 1) * BOARD_LOT
        else:
            expected_quantity = BOARD_LOT
        if quantity != expected_quantity:
            raise ReportError("planned_limit quantity does not match plan-limit sizing")
        if notional != limit_price * quantity:
            raise ReportError("planned_limit notional must equal limit price times quantity")
        if defer_reasons:
            raise ReportError("planned_limit proposal cannot retain defer reasons")
        _validate_portfolio_exposure(
            proposal,
            selected_ticker=selected_ticker,
            packet=packet,
            order_notional_yen=notional,
            warnings=warnings,
        )
    else:
        if "portfolio_exposure" in proposal:
            raise ReportError("defer proposal cannot contain portfolio_exposure")
        price_as_of = proposal.get("price_as_of")
        if price_as_of is not None and price_as_of != report_as_of.isoformat():
            raise ReportError("defer price_as_of does not match report as_of")
        if quantity != 0 or notional != 0 or proposal.get("limit_price_yen") is not None:
            raise ReportError("defer proposal must not contain an order")
        if not defer_reasons:
            raise ReportError("defer proposal requires at least one defer reason")
    return proposal


def review_bindings(
    *,
    workspace: Path,
    findings_path: Path,
    proposal_path: Path | None,
    selected_ticker: str | None,
    packet_paths: Mapping[str, Path],
    packets: Mapping[str, DecisionPacketDocument],
) -> dict[str, object]:
    """Return the compact, deterministic input binding reviewed before HTML rendering."""
    return {
        "manifest_sha256": _sha256(workspace / "manifest.yaml"),
        "findings_sha256": _sha256(findings_path),
        "comparison_sha256": _sha256(workspace / "research-comparison.yaml"),
        "packets": [
            {
                "ticker": ticker,
                "sha256": _sha256(packet_paths[ticker]),
                "core_sha256": decision_packet_core_hash(packets[ticker]),
            }
            for ticker in packet_paths
        ],
        "proposal_sha256": _sha256(proposal_path) if proposal_path is not None else None,
        "selected_ticker": selected_ticker,
    }


def _validate_review(
    *,
    review_path: Path,
    expected_bindings: Mapping[str, object],
) -> ReportReview:
    review = _load_review(review_path)
    if review.conclusion != "pass":
        raise ReportError("report review must conclude pass before HTML generation")
    actual = review.reviewed_inputs.model_dump(mode="json")
    if actual != expected_bindings:
        raise ReportError("report review input hashes are stale")
    return review


def _evidence_html(evidence: Iterable[Evidence]) -> str:
    return "".join(
        f'<li><span class="kind {item.kind}">{_esc(item.kind)}</span> '
        f"{_esc(item.statement)} <code>{_esc(', '.join(item.source_ids))}</code></li>"
        for item in evidence
    )


def _finding_html(item: Finding) -> str:
    return (
        f'<section class="finding"><h4>{_esc(item.heading)}</h4>'
        f'<p class="conclusion">{_esc(item.conclusion)}</p>'
        f"<ul>{_evidence_html(item.evidence)}</ul></section>"
    )


def _scenario_html(packet: DecisionPacketDocument) -> str:
    assert packet.estimates is not None
    rows = "".join(
        "<tr>"
        f"<td>{scenario.horizon_years}年</td><td>{_esc(scenario.name)}</td>"
        f"<td>{scenario.annual_earnings_growth_pct:+.1f}%</td>"
        f"<td>{float(scenario.terminal_valuation_multiple):,.2f}倍</td>"
        f"<td>{float(scenario.claimed_terminal_price_yen):,.0f}円</td>"
        f"<td>{float(scenario.cumulative_dividend_per_share_yen):,.0f}円</td>"
        f"<td>{scenario.claimed_total_return_cagr_pct:+.2f}%</td>"
        f"<td>{_esc(scenario.assumption)}</td></tr>"
        for scenario in packet.estimates.scenarios
    )
    return f"""<div class="scroll"><table><thead><tr><th>horizon</th><th>case</th>
<th>利益成長</th><th>terminal multiple</th><th>terminal price</th><th>累積配当</th><th>total return CAGR</th><th>assumption</th>
</tr></thead><tbody>{rows}</tbody></table></div>"""


_BREAK_EVEN_STATUS_LABELS: Mapping[str, str] = {
    "within_model_bounds": "モデル範囲内",
    "below_model_min": "モデル下限未満",
    "above_model_max": "モデル上限超過",
    "dividends_alone_sufficient": "配当のみで到達可能",
    "calculation_unresolved": "計算不能",
    "resolved": "観測値あり",
    "missing": "観測データなし",
    "ambiguous": "観測データ複数該当",
    "invalid": "観測データ不正",
    "not_applicable": "対象外（FCFEベース）",
}


def _maybe_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _break_even_html(result: FiveYearBaseBreakEvenResult | None) -> str:
    if result is None:
        return '<p class="note">break-even算出対象外。</p>'
    return f"""<div class="decision">
<p><b>要求5年CAGR:</b> {_fmt(_maybe_float(result.required_total_return_cagr_pct), digits=2, suffix="%")}</p>
<p><b>terminal multiple:</b> base {_fmt(_maybe_float(result.base_terminal_valuation_multiple), digits=2, suffix="倍")} /
break-even {_fmt(_maybe_float(result.break_even_terminal_valuation_multiple), digits=2, suffix="倍")} /
downside buffer {_fmt(_maybe_float(result.terminal_multiple_downside_buffer), digits=2, suffix="倍")}
— {_esc(_BREAK_EVEN_STATUS_LABELS[result.terminal_multiple_status])}</p>
<p><b>earnings growth:</b> base {_fmt(_maybe_float(result.base_annual_earnings_growth_pct), digits=1, suffix="%")} /
break-even {_fmt(_maybe_float(result.break_even_annual_earnings_growth_pct), digits=1, suffix="%")} /
buffer {_fmt(_maybe_float(result.earnings_growth_downside_buffer_pct_points), digits=1, suffix="pt")}
— {_esc(_BREAK_EVEN_STATUS_LABELS[result.earnings_growth_status])}</p>
<p><b>観測trailing multiple:</b> {_fmt(_maybe_float(result.observed_trailing_multiple), digits=2, suffix="倍")}
（{_esc(_BREAK_EVEN_STATUS_LABELS[result.observed_trailing_multiple_status])}）</p>
<p class="note">buffer正 = 他の仮定を固定したとき要求CAGRを守りながら吸収できる悪化余地。負でも計算としては有効で、買い提案との整合はreview済み。</p>
</div>"""


def _risks_html(packet: DecisionPacketDocument) -> str:
    return "".join(
        f"<li><b>{_esc(risk.axis)}</b> — {_esc(risk.assessment)} / "
        f"{_esc(risk.evidence_status)}: {_esc(risk.summary)}</li>"
        for risk in packet.permanent_loss_risks
    )


def _sources_html(findings: CandidateFindings, packet: DecisionPacketDocument) -> str:
    metadata = {item.source_id: item for item in findings.source_metadata}
    rows = []
    for source in packet.input_snapshot.sources:
        meta = metadata.get(source.source_id)
        title = meta.document_title if meta is not None else source.dataset or source.source_id
        published = meta.published_at.isoformat() if meta and meta.published_at else "—"
        status = meta.status if meta is not None else "ok"
        external_ref = _external_link(source.ref) if source.ref is not None else None
        if external_ref is not None:
            hostname = urlsplit(external_ref).hostname
            locator = f'<a href="{_esc(external_ref)}" target="_blank" rel="noopener noreferrer">一次資料 ({_esc(hostname)}) ↗</a>'
        elif source.ref is not None:
            locator = f"{_esc(source.provider)} / {_esc(source.dataset)} / {_esc(source.ref)}"
        else:
            locator = f"{_esc(source.provider)} / {_esc(source.dataset)}"
        rows.append(
            "<tr>"
            f"<td><code>{_esc(source.source_id)}</code></td><td>{_esc(title)}</td>"
            f"<td>{_esc(source.source_tier)} / {_esc(status)}</td><td>{published}</td>"
            f"<td>{source.as_of.isoformat()}</td><td>{source.retrieved_at.isoformat()}</td>"
            f"<td>{_esc(source.used_for)}</td><td>{_esc(meta.note) if meta and meta.note else '—'}</td>"
            f"<td>{locator}</td></tr>"
        )
    return "".join(rows)


def _candidate_html(
    *,
    findings: CandidateFindings,
    packet: DecisionPacketDocument,
    comparison: Mapping[str, object],
    break_even: FiveYearBaseBreakEvenResult | None,
) -> str:
    assert packet.estimates is not None
    questions = "".join(_finding_html(item) for item in findings.assigned_questions)
    growth = "".join(_finding_html(item) for item in findings.growth_quality)
    domains = "".join(_finding_html(item) for item in findings.domain_findings)
    catalysts = "".join(
        f"<li><b>{item.expected_on.isoformat() if item.expected_on else '日付未定'}</b> "
        f"{_esc(item.event)} — {_esc(item.decision_impact)}</li>"
        for item in findings.catalysts
    )
    unknowns = "".join(f"<li>{_esc(item)}</li>" for item in findings.unknowns)
    monitoring = "".join(f"<li>{_esc(item)}</li>" for item in findings.monitoring)
    judgment = packet.judgment
    assert judgment is not None
    ai = judgment.ai_value_capture
    return f"""
<article class="company" id="ticker-{_esc(findings.ticker)}">
<header><div><h2>{_esc(findings.ticker)} {_esc(packet.input_snapshot.company_name)}</h2>
<p>{_esc(packet.input_snapshot.sector)} / confidence: <b>{_esc(findings.confidence)}</b> / disposition: <b>{_esc(comparison["disposition"])}</b></p></div>
<a class="chart" href="{TRADINGVIEW.format(ticker=findings.ticker)}" target="_blank" rel="noopener noreferrer">TradingView ↗</a></header>
<div class="metrics"><div><span>entry basis</span><b>{float(packet.estimates.entry_price_basis_yen):,.1f}円</b></div>
<div><span>fair value</span><b>{float(packet.estimates.current_fair_value_yen):,.0f}円</b></div>
<div><span>5年base CAGR</span><b>{_fmt(comparison.get("five_year_base_cagr_pct"), digits=2, suffix="%")}</b></div>
<div><span>FV gap</span><b>{_fmt(comparison.get("fv_gap_pct"), digits=1, suffix="%")}</b></div></div>
<div class="decision"><b>比較結論:</b> {_esc(comparison["disposition_reason"])}<br>
<b>一時的mispricing仮説:</b> {_esc(comparison["temporary_mispricing_hypothesis"])}<br>
<b>portfolio追加価値:</b> {_esc(comparison["portfolio_marginal_value"])}</div>
<h3>指定質問への回答</h3>{questions}
<h3>事業モデルと価値獲得</h3><ul>{_evidence_html((findings.business_model, findings.value_capture))}</ul>
<h3>成長の質</h3>{growth}
<h3>財務耐久性</h3><ul>{_evidence_html((findings.financial_resilience,))}</ul>
{domains}
<h3>3年 / 5年 scenario</h3>{_scenario_html(packet)}
{_break_even_html(break_even)}
<h3>永久損失 7軸</h3><ul>{_risks_html(packet)}</ul>
<h3>AI value capture</h3><p>{_esc(ai.assessment_status)} / {_esc(ai.value_capture_conclusion)} / {_esc(ai.decision_weight)} — {_esc(ai.rationale)}</p>
<h3>最強countercase</h3><ul>{_evidence_html((findings.strongest_countercase,))}</ul>
<div class="split"><section><h3>Catalyst</h3><ul>{catalysts or "<li>—</li>"}</ul></section>
<section><h3>未開示・blocked</h3><ul>{unknowns}</ul></section></div>
<h3>Monitoring</h3><ul>{monitoring}</ul>
<h3>一次source</h3><div class="scroll"><table class="sources"><thead><tr><th>ID</th><th>文書</th><th>tier/status</th><th>公表日</th><th>as-of</th><th>取得時刻</th><th>used_for</th><th>decision impact</th><th>locator</th></tr></thead>
<tbody>{_sources_html(findings, packet)}</tbody></table></div>
</article>"""


def _comparison_html(
    *,
    tickers: tuple[str, ...],
    rows: Mapping[str, Mapping[str, object]],
    packets: Mapping[str, DecisionPacketDocument],
) -> str:
    body = "".join(
        "<tr>"
        f'<td><a href="{TRADINGVIEW.format(ticker=ticker)}" target="_blank" rel="noopener noreferrer">{_esc(ticker)}</a> {_esc(packets[ticker].input_snapshot.company_name)}</td>'
        f"<td>{_esc(rows[ticker]['permanent_loss_conclusion'])}</td>"
        f"<td>{_fmt(rows[ticker]['five_year_base_cagr_pct'], digits=2, suffix='%')}</td>"
        f"<td>{_fmt(rows[ticker]['fair_value_yen'], digits=0, suffix='円')}</td>"
        f"<td>{_esc(rows[ticker]['portfolio_marginal_value'])}</td>"
        f"<td>{_esc(rows[ticker]['disposition'])}</td>"
        f"<td>{_esc(rows[ticker]['disposition_reason'])}</td></tr>"
        for ticker in tickers
    )
    return f"""<div class="scroll"><table><thead><tr><th>ticker</th><th>永久損失</th>
<th>5年base CAGR</th><th>FV</th><th>portfolio追加価値</th><th>判断</th><th>理由</th></tr></thead>
<tbody>{body}</tbody></table></div>"""


def _review_findings_html(findings: Iterable[ReviewFinding]) -> str:
    items = "".join(
        "<li>"
        f'<span class="kind">{_esc(item.severity)}</span> '
        f"{_esc(item.ticker or 'all')} / {_esc(item.section)} — {_esc(item.issue)}"
        + (f" <b>required change:</b> {_esc(item.required_change)}" if item.required_change else "")
        + "</li>"
        for item in findings
    )
    return items or "<li>なし</li>"


def _portfolio_exposure_html(proposal: Mapping[str, object]) -> str:
    exposure = _exposure_mapping(
        proposal.get("portfolio_exposure"), label="proposal portfolio_exposure"
    )
    ticker = _exposure_mapping(exposure.get("ticker"), label="portfolio exposure ticker")
    sector = _exposure_mapping(exposure.get("sector"), label="portfolio exposure sector")
    factors = _dict_rows(exposure.get("common_factors"), label="portfolio exposure common_factors")
    rows = [("ticker", ticker), ("sector", sector)] + [
        ("common factor", factor) for factor in factors
    ]
    table_rows = "".join(
        "<tr>"
        f"<td>{_esc(scope)}</td>"
        f"<td>{_esc(row.get('key'))}</td>"
        f"<td>{_fmt(row.get('current_and_reserved_yen'), digits=0, suffix='円')}</td>"
        f"<td>{_fmt(row.get('prospective_yen'), digits=0, suffix='円')}</td>"
        f"<td>{_fmt(row.get('prospective_pct'), digits=2, suffix='%')}</td>"
        f"<td>{_fmt(row.get('warning_pct'), digits=2, suffix='%')}</td>"
        "</tr>"
        for scope, row in rows
    )
    fallbacks = exposure.get("ledger_fallback_tickers")
    fallback_text = (
        ", ".join(_esc(item) for item in fallbacks)
        if isinstance(fallbacks, list) and fallbacks
        else "なし"
    )
    empty_factor_tickers = exposure.get("common_factor_empty_tickers")
    empty_factor_text = (
        ", ".join(_esc(item) for item in empty_factor_tickers)
        if isinstance(empty_factor_tickers, list) and empty_factor_tickers
        else "なし"
    )
    factor_coverage_note = (
        '<p class="note">common-factor比率は、tagが付与された銘柄だけを集計したdeclared tagsベースの下限値です。</p>'
        if isinstance(empty_factor_tickers, list) and empty_factor_tickers
        else ""
    )
    return f"""<h3>portfolio exposure（同一as-of）</h3>
<dl><dt>価格basis</dt><dd>{_esc(exposure.get("price_basis"))} / {_esc(exposure.get("price_as_of"))}</dd>
<dt>holding valuation status</dt><dd>{_esc(exposure.get("holding_valuation_status"))}</dd>
<dt>total capital</dt><dd>{_fmt(exposure.get("total_capital_yen"), digits=0, suffix="円")}</dd>
<dt>ledger fallback</dt><dd>{fallback_text}</dd>
<dt>common-factor tagなし</dt><dd>{empty_factor_text}</dd></dl>
<div class="scroll"><table><thead><tr><th>scope</th><th>key</th><th>current + reserved</th>
<th>注文後</th><th>注文後比率</th><th>warning閾値</th></tr></thead><tbody>{table_rows}</tbody></table></div>
{factor_coverage_note}"""


def _nearest_dated_catalyst_html(findings: CandidateFindings | None) -> str:
    if findings is None:
        return "dated catalystなし"
    dated: list[tuple[date, str]] = sorted(
        (item.expected_on, item.event)
        for item in findings.catalysts
        if item.expected_on is not None
    )
    if not dated:
        return "dated catalystなし"
    nearest_date, nearest_event = dated[0]
    return f"{nearest_date.isoformat()} {_esc(nearest_event)}"


def _proposal_html(
    proposal: Mapping[str, object] | None,
    selected_ticker: str | None,
    *,
    candidate_findings: CandidateFindings | None,
    entry_timing: str | None,
) -> str:
    if proposal is None or selected_ticker is None:
        return """<section class="order no-order"><h2>購入提案なし</h2>
<p>全調査結果を比較した結論は no actionable bargain です。注文は作成しません。</p></section>"""
    status = str(proposal["status"])
    raw_warnings = proposal.get("warnings")
    raw_annotations = proposal.get("portfolio_annotations")
    raw_reasons = proposal.get("defer_reasons")
    warning_items = raw_warnings if isinstance(raw_warnings, list) else []
    annotation_items = raw_annotations if isinstance(raw_annotations, list) else []
    reason_items = raw_reasons if isinstance(raw_reasons, list) else []
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in warning_items)
    annotations = "".join(f"<li>{_esc(item)}</li>" for item in annotation_items)
    reasons = "".join(f"<li>{_esc(item)}</li>" for item in reason_items)
    if status == "defer":
        return f"""<section class="order no-order"><h2>購入提案なし — defer</h2>
<p>{_esc(selected_ticker)} は最良候補ですが、現在のplanning contractでは注文を作成しません。</p>
<ul>{reasons}</ul></section>"""
    return f"""<section class="order"><h2>推奨する購入方法</h2>
<p class="order-main"><b>{_esc(selected_ticker)}</b> を <b>{_fmt(proposal.get("limit_price_yen"), digits=1, suffix="円")}</b> の指値で
<b>{_fmt(proposal.get("quantity"), digits=0, suffix="株")}</b>、想定 <b>{_fmt(proposal.get("notional_yen"), digits=0, suffix="円")}</b>。</p>
<dl><dt>価格basis</dt><dd>{_esc(proposal.get("price_basis"))} / {_esc(proposal.get("price_as_of"))}</dd>
<dt>最大許容価格</dt><dd>{_fmt(proposal.get("max_acceptable_price_yen"), digits=1, suffix="円")}</dd>
<dt>board lot</dt><dd>{_fmt(proposal.get("board_lot"), digits=0, suffix="株")}</dd>
<dt>有効期限</dt><dd>{_esc(proposal.get("expires_at"))}</dd>
<dt>entry timing</dt><dd>{_esc(entry_timing) if entry_timing is not None else "—"}</dd>
<dt>直近の確認event</dt><dd>{_nearest_dated_catalyst_html(candidate_findings)}</dd></dl>
{_portfolio_exposure_html(proposal)}
<h3>warning</h3><ul>{warnings or "<li>なし</li>"}</ul>
<h3>portfolio annotation</h3><ul>{annotations or "<li>なし</li>"}</ul>
<p class="note">これは人間の注文判断のためのplanning-only提案であり、brokerへ発注しません。</p></section>"""


def render(
    *,
    workspace: Path,
    findings_path: Path,
    review_path: Path,
    proposal_path: Path | None = None,
) -> str:
    findings = _load_findings(findings_path)
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_manifest_inputs(manifest)
    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    if str(manifest.get("as_of")) != findings.meta.as_of.isoformat():
        raise ReportError("findings as_of does not match workspace manifest")
    shortlist = _dict_rows(selection.get("shortlist"), label="selection shortlist")
    shortlist_tickers = tuple(str(row.get("ticker")) for row in shortlist)
    findings_by = {item.ticker: item for item in findings.candidates}
    if set(shortlist_tickers) != set(findings_by):
        raise ReportError("findings tickers must exactly match selection.shortlist")
    rows = _comparison_index(comparison)
    packets: dict[str, DecisionPacketDocument] = {}
    packet_paths: dict[str, Path] = {}
    break_evens: dict[str, FiveYearBaseBreakEvenResult | None] = {}
    for ticker in shortlist_tickers:
        if ticker not in rows:
            raise ReportError(f"comparison is missing shortlist ticker {ticker}")
        _require_completed_comparison(rows[ticker], ticker)
        packet_path, packet = _load_packet(workspace, ticker)
        _validate_comparison_values(rows[ticker], packet, ticker)
        if packet.input_snapshot.as_of != findings.meta.as_of:
            raise ReportError(f"packet as_of mismatch for {ticker}")
        _validate_sources(findings_by[ticker], packet)
        try:
            packet_result = evaluate_decision_packet(packet)
        except DecisionPacketError as error:
            raise ReportError(f"cannot evaluate decision packet for {ticker}: {error}") from error
        packet_paths[ticker] = packet_path
        packets[ticker] = packet
        break_evens[ticker] = packet_result.five_year_base_break_even
    selected = comparison.get("selected_ticker")
    selected_ticker = str(selected) if selected is not None else None
    if selected_ticker is not None and selected_ticker not in shortlist_tickers:
        raise ReportError("selected_ticker is not in the research shortlist")
    if not isinstance(comparison.get("ranking_rationale"), str):
        raise ReportError("research comparison needs ranking_rationale")
    _validate_selection_dispositions(rows, selected_ticker)
    proposal = _validate_proposal(
        proposal_path=proposal_path,
        selected_ticker=selected_ticker,
        packet_path=packet_paths.get(selected_ticker) if selected_ticker else None,
        packet=packets.get(selected_ticker) if selected_ticker else None,
        manifest=manifest,
        report_as_of=findings.meta.as_of,
        report_budget_yen=findings.meta.budget_yen,
        target_session=findings.meta.target_session,
    )
    if selected_ticker is not None and findings.decision_context.entry_timing is None:
        raise ReportError("decision_context.entry_timing is required when a proposal exists")
    bindings = review_bindings(
        workspace=workspace,
        findings_path=findings_path,
        proposal_path=proposal_path,
        selected_ticker=selected_ticker,
        packet_paths=packet_paths,
        packets=packets,
    )
    review = _validate_review(review_path=review_path, expected_bindings=bindings)
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in findings.meta.warnings)
    companies = "".join(
        _candidate_html(
            findings=findings_by[ticker],
            packet=packets[ticker],
            comparison=rows[ticker],
            break_even=break_evens[ticker],
        )
        for ticker in shortlist_tickers
    )
    provenance = "".join(
        f"<li>{_esc(path.relative_to(workspace.resolve()))}: <code>{_sha256(path)}</code></li>"
        for path in packet_paths.values()
    )
    proposal_hash = (
        f"<li>{_esc(proposal_path)}: <code>{_sha256(proposal_path)}</code></li>"
        if proposal_path is not None
        else ""
    )
    return _TEMPLATE.format(
        title=_esc(findings.meta.title),
        as_of=findings.meta.as_of.isoformat(),
        target=findings.meta.target_session.isoformat(),
        budget=f"{findings.meta.budget_yen:,}",
        freshness=_esc(findings.meta.source_freshness),
        warnings=warnings or "<li>なし</li>",
        selected=_esc(selected_ticker or "なし"),
        ranking=_esc(comparison["ranking_rationale"]),
        portfolio_fit=_esc(findings.decision_context.portfolio_fit),
        human_action=_esc(findings.decision_context.human_action),
        proposal=_proposal_html(
            proposal,
            selected_ticker,
            candidate_findings=findings_by.get(selected_ticker) if selected_ticker else None,
            entry_timing=findings.decision_context.entry_timing,
        ),
        comparison=_comparison_html(tickers=shortlist_tickers, rows=rows, packets=packets),
        companies=companies,
        manifest_hash=_sha256(workspace / "manifest.yaml"),
        comparison_hash=_sha256(workspace / "research-comparison.yaml"),
        findings_hash=_sha256(findings_path),
        review_identity=_esc(review.reviewer_identity),
        review_run_id=_esc(review.reviewer_run_id),
        review_hash=_sha256(review_path),
        review_findings=_review_findings_html(review.findings),
        packet_hashes=provenance,
        proposal_hash=proposal_hash,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the detailed research decision report.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--findings", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        document = render(
            workspace=args.workspace,
            findings_path=args.findings,
            review_path=args.review,
            proposal_path=args.proposal,
        )
    except ReportError as error:
        parser.error(str(error))
    write_text_atomic(args.out, document)
    print(f"wrote {args.out} ({len(document)} bytes)")
    return 0


_TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; script-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>{title}</title><style>
:root{{--bg:#f4f5f7;--card:#fff;--ink:#17202a;--muted:#64748b;--line:#dbe2ea;--accent:#155e75;--good:#166534;--warn:#9a3412}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111827;--card:#1f2937;--ink:#f3f4f6;--muted:#9ca3af;--line:#374151;--accent:#67e8f9;--good:#86efac;--warn:#fdba74}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Noto Sans JP",Meiryo,sans-serif}}
.wrap{{max-width:1400px;margin:auto;padding:32px 24px 80px}}h1{{font-size:30px;margin:0}}h2{{margin:42px 0 14px;border-bottom:2px solid var(--line);padding-bottom:7px}}h3{{font-size:17px;margin:28px 0 8px}}h4{{margin:0 0 5px;color:var(--accent)}}
a{{color:var(--accent)}}code{{font-size:11px;background:color-mix(in srgb,var(--ink) 8%,transparent);padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}}
.lede,.note{{color:var(--muted)}}.summary,.order,.company{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin:22px 0}}
.order{{border:3px solid var(--accent)}}.no-order{{border-color:var(--warn)}}.order-main{{font-size:21px}}dl{{display:grid;grid-template-columns:150px 1fr;gap:5px 12px}}dt{{color:var(--muted)}}
.company header{{display:flex;justify-content:space-between;align-items:start;gap:20px}}.company header h2{{border:0;margin:0;padding:0}}.chart{{white-space:nowrap}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}}.metrics div{{border:1px solid var(--line);border-radius:9px;padding:10px}}.metrics span{{display:block;color:var(--muted);font-size:12px}}.metrics b{{font-size:18px}}
.decision,.finding{{border-left:4px solid var(--accent);padding:10px 14px;margin:12px 0;background:color-mix(in srgb,var(--accent) 6%,transparent)}}.finding ul{{margin:6px 0}}.conclusion{{font-weight:600}}
.kind{{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 7px}}.kind.management_claim{{color:var(--warn)}}.kind.observed{{color:var(--good)}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:780px;font-size:13px}}th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted)}}
.sources{{min-width:1100px}}.split{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.provenance{{font-size:12px;color:var(--muted)}}
@media(max-width:800px){{.metrics{{grid-template-columns:1fr 1fr}}.split{{grid-template-columns:1fr}}.wrap{{padding:20px 12px}}}}
</style></head><body><main class="wrap"><h1>{title}</h1>
<p class="lede">as-of {as_of} / 注文想定日 {target} / 予算 {budget}円。これは人間判断用のHTML projectionであり、packet・review・plan-limit・ledgerを置き換えません。</p>
<p class="lede">内容レビュー: pass / reviewer={review_identity} / run={review_run_id} / review sha256=<code>{review_hash}</code></p>
<section class="summary"><h2>Executive decision</h2><p><b>最終候補:</b> {selected}</p><p><b>比較理由:</b> {ranking}</p><p><b>portfolio fit:</b> {portfolio_fit}</p><p><b>人間への依頼:</b> {human_action}</p>
<p><b>source freshness:</b> {freshness}</p><h3>未解決warning</h3><ul>{warnings}</ul>
<h3>content review finding</h3><ul>{review_findings}</ul></section>
{proposal}<h2>全候補の横比較</h2>{comparison}{companies}
<section class="provenance"><h2>Provenance</h2><ul><li>manifest: <code>{manifest_hash}</code></li><li>research-comparison: <code>{comparison_hash}</code></li><li>findings: <code>{findings_hash}</code></li>{packet_hashes}{proposal_hash}</ul></section>
</main></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
