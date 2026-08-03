"""Read-only CLI commands over cached data: profiles, snapshots, selection."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.appdb import database_path
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.macro.context import (
    MacroContext,
    fired_trigger_summaries,
    macro_context_from_payload,
)
from baibai_engine.macro.indicators.db import DEFAULT_DB_PATH as INDICATORS_DB_PATH
from baibai_engine.market.store import latest_daily_bar_date
from baibai_engine.read_api.macro import latest_macro_context_payload, macro_context_payload
from baibai_engine.screening.market_snapshot import build_market_snapshot
from baibai_engine.screening.regime import MarketRegimeSnapshot, compute_market_regime
from baibai_engine.screening.rule_config import (
    DEFAULT_RULES_PATH,
    ScreeningRules,
    load_screening_rules,
)
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.schema import (
    normalize_ticker,
)
from baibai_engine.screening.selection import (
    CandidateRecord,
    PreviousCandidates,
    build_selection_payload,
    candidate_record_from_mapping,
    load_previous_longlist,
)
from baibai_engine.screening.ticker_profile import build_ticker_profile

from .common import _NoAliasDumper, _parse_iso_date


def ticker_profile_command(
    *,
    ticker: str,
    asof: str | None,
    sqlite_path: Path,
    runs_db_path: Path,
    app_db_path: Path,
    run_revision_id: str | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        normalized = normalize_ticker(ticker)
    except (ValueError, sqlite3.Error) as exc:
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
    try:
        payload = build_ticker_profile(
            sqlite_path=sqlite_path,
            ticker=normalized,
            asof_date=asof_date,
            runs_db_path=runs_db_path,
            app_db_path=database_path(app_db_path),
            run_revision_id=run_revision_id,
        )
    except (ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
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
    top: int,
    rules: ScreeningRules | None = None,
    profile: str | None = None,
    detail: str = "summary",
    longlist_top: int = 0,
    output_path: Path | None = None,
    force: bool = False,
    regime_sqlite_path: Path | None = None,
    stdout: TextIO | None = None,
    run_revision_id: str | None = None,
    runs_db_path: Path | None = None,
    app_db_path: Path | None = None,
    macro_context_id: str | None = None,
    previous_run_revision_id: str | None = None,
    longlist_history_dir: Path | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1
    if not 0 <= longlist_top <= 100:
        print("--longlist-top must be between 0 and 100", file=sys.stderr)
        return 1
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    rules = rules or load_screening_rules(_rules_path_from_env())
    try:
        if run_revision_id is None:
            raise ValueError("run_revision_id is required")
        inputs = _load_selection_inputs_db(
            asof_date=asof_date,
            run_revision_id=run_revision_id,
            runs_db_path=runs_db_path,
            app_db_path=app_db_path,
            macro_context_id=macro_context_id,
            previous_run_revision_id=previous_run_revision_id,
            longlist_history_dir=longlist_history_dir,
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
            market_regime=_load_market_regime(regime_sqlite_path, asof_date),
            detail=detail,
            longlist_top=longlist_top,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    selection = payload.get("selection")
    if not isinstance(selection, Mapping):  # pragma: no cover - builder invariant
        raise AssertionError("selection payload must contain metadata")
    effective_profile = str(selection["profile"])
    try:
        publication = ScreeningRunStore(runs_db_path).publish_selection(
            run_revision_id=run_revision_id,
            profile=effective_profile,
            macro_context_id=inputs.macro_context_ref,
            payload=payload,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"screening selection publication failed: {exc}", file=sys.stderr)
        return 1
    payload = {"selection_id": publication.publication_id, **payload}
    rendered = yaml.dump(payload, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    # --output-path 指定時は同じ内容を file と stdout の両方へ出す。file は
    # local/rebuildable な保存先で、canonical 判断は thesis だけが担う。
    if output_path is not None:
        write_text_atomic(output_path, rendered)
    out.write(rendered)
    return 0


def selection_show_command(
    *,
    selection_id: str,
    runs_db_path: Path | None = None,
    output_path: Path | None = None,
    force: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """Re-emit a published selection in the shape ``select`` wrote it.

    Reading is the only way to recover a selection output after retention has
    evicted the run it was bound to. Re-running ``select`` would publish a second
    selection instead, leaving the shortlist bound to one and the research
    workspace built from another.
    """
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    try:
        publication = ScreeningRunReader(runs_db_path).get_selection(selection_id)
    except (OSError, sqlite3.Error) as exc:
        print(f"screening run store is unreadable: {exc}", file=sys.stderr)
        return 1
    if publication is None:
        # No reconstruction from candidates: a selection that is not stored was
        # never published, and guessing one would fabricate a decision input.
        print(f"selection not found: {selection_id}", file=sys.stderr)
        return 1

    payload = {"selection_id": publication.selection_id, **publication.payload}
    rendered = yaml.dump(payload, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    if output_path is not None:
        write_text_atomic(output_path, rendered)
    out.write(rendered)
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
    candidates_ref: str
    macro_context_ref: str | None


def _load_selection_inputs_db(
    *,
    asof_date: date,
    run_revision_id: str,
    runs_db_path: Path | None,
    app_db_path: Path | None,
    macro_context_id: str | None,
    previous_run_revision_id: str | None,
    longlist_history_dir: Path | None,
) -> _SelectionInputs:
    reader = ScreeningRunReader(runs_db_path)
    run = reader.get_run(run_revision_id)
    if run is None:
        raise ValueError(f"unknown run_revision_id: {run_revision_id}")
    if run.as_of_date != asof_date.isoformat():
        raise ValueError(
            f"run revision as-of mismatch: {run.as_of_date} != {asof_date.isoformat()}"
        )
    candidate_records = tuple(candidate_record_from_mapping(item) for item in run.candidates)
    if previous_run_revision_id is None:
        previous = reader.previous_run(before_as_of_date=run.as_of_date)
    else:
        previous = reader.get_run(previous_run_revision_id)
        if previous is None:
            raise ValueError(f"unknown previous_run_revision_id: {previous_run_revision_id}")
        prior_as_of = reader.latest_as_of_before(run.as_of_date)
        if prior_as_of is None or previous.as_of_date != prior_as_of:
            raise ValueError(
                "previous run revision must belong to the greatest as-of before the current run"
            )
    if previous is not None:
        previous_candidates = PreviousCandidates(
            ref_path=previous.run_revision_id,
            source="run_revision",
            tickers=tuple(str(item["ticker"]) for item in previous.candidates),
        )
    elif longlist_history_dir is not None:
        # The prior as-of has been pruned out of the run store. The persisted daily
        # longlists outlive that retention, so they can still supply an earlier side
        # for the overlap diagnostic and the previous-candidate cap.
        previous_candidates = load_previous_longlist(longlist_history_dir, asof_date=asof_date)
    else:
        previous_candidates = PreviousCandidates(ref_path=None, source=None, tickers=())
    resolved_app_db = database_path(app_db_path)
    if macro_context_id is None:
        context_payload = latest_macro_context_payload(resolved_app_db, as_of=asof_date)
    else:
        context_payload = macro_context_payload(
            resolved_app_db,
            context_id=macro_context_id,
            as_of=asof_date,
        )
    context = (
        None
        if context_payload is None
        else _with_fired_triggers(
            macro_context_from_payload(
                context_payload,
                source=str(context_payload["context_id"]),
            ),
            application_db=resolved_app_db,
            asof_date=asof_date,
        )
    )
    return _SelectionInputs(
        candidates=candidate_records,
        macro_context=context,
        previous_candidates=previous_candidates,
        candidates_ref=run_revision_id,
        macro_context_ref=None if context is None else context.context_id,
    )


def _with_fired_triggers(
    context: MacroContext,
    *,
    application_db: Path,
    asof_date: date,
) -> MacroContext:
    """Ask the L1 store whether the report's own invalidation conditions have been met.

    The evaluation happens here because this is the layer that knows where both stores
    are; everything downstream stays a pure read of the context object.
    """

    fired = fired_trigger_summaries(
        context_db=application_db,
        indicators_db_path=INDICATORS_DB_PATH,
        context_id=context.context_id,
        asof=asof_date,
    )
    if fired is None:
        # No indicator store answered. Recording that is what keeps "nothing fired" from
        # reading the same as "nobody looked".
        return context
    return replace(context, fired_triggers=fired, triggers_checked=True)


def _rules_path_from_env() -> Path:
    return Path(os.environ.get("SCREENING_RULES_PATH") or DEFAULT_RULES_PATH)
