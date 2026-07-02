"""Read-only CLI commands over cached data: profiles, snapshots, selection."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml

from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.macro.context import MacroContext, find_latest_macro_context, load_macro_context
from baibai_loop.market.store import latest_daily_bar_date
from baibai_loop.screening.market_snapshot import build_market_snapshot
from baibai_loop.screening.regime import MarketRegimeSnapshot, compute_market_regime
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
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
)
from baibai_loop.screening.ticker_profile import build_ticker_profile

from .common import _NoAliasDumper, _parse_iso_date


def ticker_profile_command(
    *,
    ticker: str,
    asof: str | None,
    sqlite_path: Path,
    candidates_root: Path,
    records_root: Path,
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
        records_root=records_root,
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
            market_regime=_load_market_regime(regime_sqlite_path, asof_date),
            detail=detail,
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
        candidate_records = _load_candidate_records(candidates_path)
    except ValueError as exc:
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
    prior_research = load_prior_research(repo_root / "records/05-thesis", asof_date)
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
            f"using current working directory for prior decision register: {Path.cwd()}",
            file=sys.stderr,
        )
    return Path.cwd()


def _rules_path_from_env() -> Path:
    return Path(os.environ.get("SCREENING_RULES_PATH") or DEFAULT_RULES_PATH)


def _load_candidate_records(path: Path) -> tuple[CandidateRecord, ...]:
    """Parse candidates YAML through the one selection-side parsing path.

    Field coercion lives in ``candidate_record_from_mapping`` (shared with the
    replay loaders), so new screen facts never need a second registration here.
    Only the structural shape is checked.
    """
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidates YAML root must be a mapping")
    raw_candidates = payload.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates must be a list")
    records: list[CandidateRecord] = []
    for index, item in enumerate(raw_candidates):
        if not isinstance(item, Mapping) or not str(item.get("ticker") or ""):
            raise ValueError(f"candidates[{index}] must be a mapping with a ticker")
        records.append(candidate_record_from_mapping(item))
    return tuple(records)
