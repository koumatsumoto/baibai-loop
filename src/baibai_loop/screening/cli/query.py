"""Read-only CLI commands over cached data: profiles, snapshots, selection."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from baibai_loop.macro_context import MacroContext, find_latest_macro_context, load_macro_context
from baibai_loop.screening.market_snapshot import build_market_snapshot
from baibai_loop.screening.regime import MarketRegimeSnapshot, compute_market_regime
from baibai_loop.screening.render import JST
from baibai_loop.screening.rule_config import (
    DEFAULT_RULES_PATH,
    ScreeningRules,
    load_screening_rules,
)
from baibai_loop.screening.schema import (
    normalize_ticker,
)
from baibai_loop.screening.selection import (
    CandidateRecord,
    PreviousCandidates,
    PriorResearch,
    build_selection_payload,
    build_selection_sweep_payload,
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
    load_profile_overrides,
)
from baibai_loop.screening.sqlite_reader import latest_daily_bar_date
from baibai_loop.screening.ticker_profile import build_ticker_profile

from .common import _NoAliasDumper, _parse_iso_date

type MetricScalar = bool | date | datetime | float | int | str | None


class _ScreenedCandidateInput(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ticker: str
    name: str | None = None
    per_forward: int | float | None = None
    per_trailing: int | float | None = None
    pbr: int | float | None = None
    ev_ebitda: int | float | None = None
    p_s: int | float | None = None
    pcfr: int | float | None = None
    sector_33: str = ""
    market_cap_oku: int | float | None = None
    avg_turnover_oku: int | float | None = None
    listing_span_days: int | None = None
    jpx_flags: list[str] = Field(default_factory=list)
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    sector_relative_strength_percentile: float | None = None
    evidence_hits: list[dict[str, object]] = Field(default_factory=list)
    metrics: dict[str, MetricScalar] = Field(default_factory=dict)
    freshness_warnings: list[dict[str, object]] = Field(default_factory=list)
    next_earnings_date: str | None = None
    split_adjustment_flag: bool | None = None
    ttm_quality: dict[str, object] = Field(default_factory=dict)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


class _ScreenedFrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    candidates: list[_ScreenedCandidateInput] = Field(default_factory=list)


def ticker_profile_command(
    *,
    ticker: str,
    asof: str | None,
    sqlite_path: Path,
    candidates_root: Path,
    ledger_root: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        normalized = normalize_ticker(ticker)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if asof is not None:
        asof_date = _parse_iso_date(asof)
    else:
        today = datetime.now(JST).date()
        resolved = latest_daily_bar_date(sqlite_path, today - timedelta(days=30), today)
        if resolved is None:
            print(
                f"no cached daily bars found to resolve --asof: {sqlite_path}",
                file=sys.stderr,
            )
            return 1
        asof_date = resolved
    payload = build_ticker_profile(
        sqlite_path=sqlite_path,
        ticker=normalized,
        asof_date=asof_date,
        candidates_root=candidates_root,
        ledger_root=ledger_root,
    )
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def market_snapshot_command(
    *,
    asof: str | None,
    weeks: int,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    if weeks < 1:
        print("--weeks must be greater than zero", file=sys.stderr)
        return 1
    if asof is not None:
        asof_date = _parse_iso_date(asof)
    else:
        today = datetime.now(JST).date()
        resolved = latest_daily_bar_date(sqlite_path, today - timedelta(days=30), today)
        if resolved is None:
            print(
                f"no cached daily bars found to resolve --asof: {sqlite_path}",
                file=sys.stderr,
            )
            return 1
        asof_date = resolved
    payload = build_market_snapshot(
        sqlite_path=sqlite_path,
        asof_date=asof_date,
        history_weeks=weeks,
    )
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def select_command(
    *,
    asof_date: date,
    macro_context_path: Path | None,
    candidates_path: Path | None = None,
    top: int,
    candidates_root: Path | None = None,
    macro_context_root: Path | None = None,
    rules: ScreeningRules | None = None,
    profile: str | None = None,
    profile_config_path: Path | None = None,
    detail: str = "summary",
    regime_sqlite_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    rules = rules or load_screening_rules(_rules_path_from_env())
    try:
        inputs = _load_selection_inputs(
            asof_date=asof_date,
            candidates_path=candidates_path,
            macro_context_path=macro_context_path,
            candidates_root=candidates_root,
            macro_context_root=macro_context_root,
        )
        profile_overrides = load_profile_overrides(profile_config_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        payload = build_selection_payload(
            asof_date=asof_date,
            candidates=inputs.candidates,
            macro_context=inputs.macro_context,
            rules=rules,
            top=top,
            profile=profile,
            candidates_ref=inputs.candidates_ref,
            macro_context_ref=inputs.macro_context_ref,
            previous_candidates=inputs.previous_candidates,
            prior_research_by_ticker=inputs.prior_research,
            profile_overrides=profile_overrides,
            market_regime=_load_market_regime(regime_sqlite_path, asof_date),
            detail=detail,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def select_sweep_command(
    *,
    asof_date: date,
    macro_context_path: Path | None,
    candidates_path: Path | None = None,
    top: int,
    profiles: Sequence[str],
    candidates_root: Path | None = None,
    macro_context_root: Path | None = None,
    rules: ScreeningRules | None = None,
    profile_config_path: Path | None = None,
    regime_sqlite_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1
    if not profiles:
        print("--profiles must include at least one profile", file=sys.stderr)
        return 1
    out = stdout if stdout is not None else sys.stdout
    rules = rules or load_screening_rules(_rules_path_from_env())
    try:
        inputs = _load_selection_inputs(
            asof_date=asof_date,
            candidates_path=candidates_path,
            macro_context_path=macro_context_path,
            candidates_root=candidates_root,
            macro_context_root=macro_context_root,
        )
        profile_overrides = load_profile_overrides(profile_config_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        payload = build_selection_sweep_payload(
            asof_date=asof_date,
            candidates=inputs.candidates,
            macro_context=inputs.macro_context,
            rules=rules,
            top=top,
            profiles=profiles,
            candidates_ref=inputs.candidates_ref,
            macro_context_ref=inputs.macro_context_ref,
            previous_candidates=inputs.previous_candidates,
            prior_research_by_ticker=inputs.prior_research,
            profile_overrides=profile_overrides,
            market_regime=_load_market_regime(regime_sqlite_path, asof_date),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    yaml.dump(payload, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def _load_market_regime(
    regime_sqlite_path: Path | None,
    asof_date: date,
) -> MarketRegimeSnapshot | None:
    # A missing cache silently disables the lens (selection stays usable on a
    # checkout without market.sqlite); the diagnostics record market_regime: null
    # so the degraded mode is visible in the output.
    if regime_sqlite_path is None:
        return None
    return compute_market_regime(regime_sqlite_path, asof_date)


@dataclass(frozen=True, slots=True)
class _SelectionInputs:
    candidates: tuple[CandidateRecord, ...]
    macro_context: MacroContext | None
    previous_candidates: PreviousCandidates
    prior_research: Mapping[str, PriorResearch]
    candidates_ref: str
    macro_context_ref: str | None


def _load_selection_inputs(
    *,
    asof_date: date,
    candidates_path: Path | None,
    macro_context_path: Path | None,
    candidates_root: Path | None,
    macro_context_root: Path | None,
) -> _SelectionInputs:
    resolved_candidates_root = candidates_root or Path("records/04-candidates")
    resolved_macro_context_root = macro_context_root or Path("records/01-macro-context")
    candidates_path = candidates_path or (
        resolved_candidates_root
        / f"{asof_date:%Y}"
        / f"{asof_date:%m}"
        / f"{asof_date:%Y-%m-%d}.yaml"
    )
    if not candidates_path.exists():
        raise ValueError(f"candidates file not found: {candidates_path}")
    try:
        candidates_fm = TypeAdapter(_ScreenedFrontMatter).validate_python(
            _parse_candidates_yaml_payload(candidates_path)
        )
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"invalid candidates YAML: {candidates_path}: {exc}") from exc

    resolved_macro_context_path = macro_context_path or find_latest_macro_context(
        resolved_macro_context_root, asof_date
    )
    if resolved_macro_context_path is None or not resolved_macro_context_path.exists():
        raise ValueError(
            "macro context file not found. Pass --macro-context <path> or create "
            "records/01-macro-context/<YYYY>/<MM>/macro-context-*.yaml"
        )
    try:
        macro_context = load_macro_context(resolved_macro_context_path)
    except ValueError as exc:
        raise ValueError(f"invalid macro context: {resolved_macro_context_path}: {exc}") from exc
    if macro_context.as_of > asof_date:
        raise ValueError(
            "macro context as_of is after screening asof; create an asof-appropriate "
            f"context or choose a later --asof: {macro_context.as_of.isoformat()} > "
            f"{asof_date.isoformat()}"
        )
    if macro_context.is_stale_for(asof_date):
        raise ValueError(
            "macro context is stale for screening asof; refresh macro context before "
            f"screening: valid_until {macro_context.valid_until.isoformat()} < "
            f"{asof_date.isoformat()}"
        )

    repo_root = _repository_root_from_records_anchor(
        resolved_macro_context_path, warn_on_fallback=True
    )
    previous_candidates = load_previous_candidates(
        resolved_candidates_root,
        asof_date,
        current_path=candidates_path,
    )
    prior_research = load_prior_research(
        repo_root / "records/_ledger/research-decisions", asof_date
    )
    candidate_records: tuple[CandidateRecord, ...] = tuple(
        candidate_record_from_mapping(item.model_dump(mode="python"))
        for item in candidates_fm.candidates
    )
    return _SelectionInputs(
        candidates=candidate_records,
        macro_context=macro_context,
        previous_candidates=previous_candidates,
        prior_research=prior_research,
        candidates_ref=_repository_relative_ref(
            candidates_path,
            anchor=resolved_macro_context_path,
        ),
        macro_context_ref=_repository_relative_ref(
            resolved_macro_context_path,
            anchor=resolved_macro_context_path,
        ),
    )


def _repository_relative_ref(path: Path, *, anchor: Path) -> str:
    repo_root = _repository_root_from_records_anchor(anchor)
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _repository_root_from_records_anchor(path: Path, *, warn_on_fallback: bool = False) -> Path:
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == "records":
            return parent.parent
    if warn_on_fallback:
        print(
            "warning: could not infer repository root from a records/ anchor; "
            f"using current working directory for prior research ledger: {Path.cwd()}",
            file=sys.stderr,
        )
    return Path.cwd()


def _rules_path_from_env() -> Path:
    return Path(os.environ.get("SCREENING_RULES_PATH") or DEFAULT_RULES_PATH)


def _parse_candidates_yaml_payload(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"candidates YAML root must be a mapping: {path}")
    return payload
