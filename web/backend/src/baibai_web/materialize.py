"""Export the serving read-model JSON tree (views/ + history/) from local stores.

Runs the same ``baibai_web.readmodel`` builders as the local FastAPI app, so each
``views/*.json`` file carries the same JSON shape as the corresponding ``/api``
response. The output directory is what a scheduled batch uploads to the serving
object store; no business logic exists beyond these builders.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from baibai_engine.read_api import (
    MACRO_READING_RULES_PATH,
    MacroGranularity,
    MaterializationPreconditionError,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
    screening_run_asof_dates,
    validate_application_store_schema,
    validate_macro_reading_rules,
    validate_market_store_hydration,
)
from baibai_web.readmodel.builders import (
    MacroPeriod,
    build_assessment_detail,
    build_daily_delta,
    build_dashboard,
    build_macro,
    build_macro_context_detail,
    build_macro_reading,
    build_meta,
    build_operations_view,
    build_screening,
    build_screening_history_run,
    build_security_detail,
)
from baibai_web.readmodel.models import DashboardView, MetaBatch, ScreeningView
from baibai_web.sources.db_sources import DbCandidatesSource
from baibai_web.sources.factory import build_sources
from baibai_web.sources.protocols import LedgerSource, ResearchSource
from baibai_web.sources.types import CandidatesRun

_JST = ZoneInfo("Asia/Tokyo")
# Read off the builder's own literals rather than restated here: a window added there is
# exported by the same edit, instead of becoming a view file the UI asks for and never finds.
_MACRO_PERIODS: tuple[MacroPeriod, ...] = get_args(MacroPeriod.__value__)
_MACRO_GRANULARITIES: tuple[MacroGranularity, ...] = get_args(MacroGranularity.__value__)
# Same shape the ledger and run store enforce at write time; re-checked here so a
# store-derived string never reaches filename composition unvalidated.
_TICKER_FORMAT = re.compile(r"[0-9A-Z]{4}")
# macro context_id charset; excludes path separators so it is safe in a filename
# and mirrors the Worker route validation for the same key.
_MACRO_CONTEXT_ID_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_ASSESSMENT_ID_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,128}")


ExportPreconditionError = MaterializationPreconditionError


class _RankedSetHistoryMember(BaseModel):
    ticker: str
    rank: int | None
    er_annual: float | None


class _RankedSetHistoryRecord(BaseModel):
    kind: Literal["daily-ranked-set-membership"] = "daily-ranked-set-membership"
    schema_version: Literal[1] = 1
    as_of: date
    run_revision_id: str
    selection_status: Literal["available", "selection_missing"]
    selection_id: str | None
    selection_created_at: datetime | None
    members: list[_RankedSetHistoryMember]


def export_read_models(
    root: Path,
    output_dir: Path,
    *,
    batch: MetaBatch | None = None,
) -> list[Path]:
    """Write every serving JSON under ``output_dir`` and return the written paths.

    ``views/`` is recreated from scratch so it always carries the complete image of
    one export and no stale per-ticker view survives a shrinking target set;
    ``history/`` only appends. ``views/meta.json`` is written last so its freshness
    claim exists only after every other file has been written successfully.
    """

    stores = build_sources(root)
    validate_application_store_schema(stores.app_db_path)
    validate_macro_reading_rules(root / MACRO_READING_RULES_PATH)
    validate_market_store_hydration(stores.market_db_path)
    views_dir = output_dir / "views"
    if views_dir.is_dir():
        shutil.rmtree(views_dir)
    views_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    dashboard = build_dashboard(
        stores.ledger,
        stores.research,
        stores.tasks,
        stores.candidates,
        stores.market,
    )
    written.append(_write_model(views_dir / "dashboard.json", dashboard))

    screening = build_screening(
        stores.candidates,
        stores.ledger,
        stores.research,
        stores.er_level_calibration,
    )
    written.append(_write_model(views_dir / "screening_latest.json", screening))

    written.append(
        _write_model(
            views_dir / "daily-delta.json",
            build_daily_delta(
                stores.candidates,
                stores.ledger,
                stores.research,
                stores.market,
                stores.macro,
            ),
        )
    )

    written.append(
        _write_model(views_dir / "operations.json", build_operations_view(stores.operations))
    )

    as_of = datetime.now(_JST).date()
    reading = build_macro_reading(stores.macro, asof=as_of)
    if reading is None:
        # The Macro tab degrades to hiding the panel; a missing indicator store must not
        # fail the whole export.
        _warn("macro reading is unavailable; views/macro-reading.json skipped")
    else:
        written.append(_write_model(views_dir / "macro-reading.json", reading))

    for period in _MACRO_PERIODS:
        for granularity in _MACRO_GRANULARITIES:
            macro = build_macro(stores.macro, as_of=as_of, period=period, granularity=granularity)
            written.append(_write_model(views_dir / f"macro--{period}-{granularity}.json", macro))

    for context in stores.macro.contexts():
        context_id = str(context["context_id"])
        if _MACRO_CONTEXT_ID_FORMAT.fullmatch(context_id) is None:
            _warn(f"macro context_id has an unexpected format: {context_id!r}; report view skipped")
            continue
        try:
            detail = build_macro_context_detail(stores.macro, context_id=context_id, as_of=as_of)
        except ValueError as error:
            # A not-yet-eligible (future as_of) revision is 404 on the API too; skip it
            # rather than fail the whole batch.
            _warn(f"macro report view skipped for {context_id!r}: {error}")
            continue
        written.append(_write_model(views_dir / f"macro-context--{context_id}.json", detail))

    for summary in screening.assessments:
        assessment_id = summary.assessment_id
        if _ASSESSMENT_ID_FORMAT.fullmatch(assessment_id) is None:
            _warn(f"assessment_id has an unexpected format: {assessment_id!r}; view skipped")
            continue
        assessment = build_assessment_detail(stores.candidates, assessment_id=assessment_id)
        if assessment is None:  # pragma: no cover - the index comes from the same store
            _warn(f"bargain assessment view is unavailable for {assessment_id!r}; skipped")
            continue
        written.append(_write_model(views_dir / f"assessment--{assessment_id}.json", assessment))

    cached_candidates = _CachedLatestRunCandidates(stores.candidates)
    for ticker in _security_tickers(dashboard, screening):
        if _TICKER_FORMAT.fullmatch(ticker) is None:
            _warn(f"ticker has an unexpected format: {ticker!r}; security view skipped")
            continue
        security = build_security_detail(
            ticker,
            stores.ledger,
            stores.research,
            cached_candidates,
            stores.market,
        )
        if security is None:
            _warn(f"security view is unavailable for ticker {ticker}; skipped")
            continue
        written.append(_write_model(views_dir / f"security--{ticker}.json", security))

    written.extend(
        _write_history(
            output_dir,
            screening,
            candidates=stores.candidates,
            ledger=stores.ledger,
            research=stores.research,
            runs_db_path=stores.runs_db_path,
        )
    )
    ranked_set_history = _ranked_set_history_record(stores.candidates)
    if ranked_set_history is not None:
        written.append(
            _write_model(
                output_dir / "history/ranked_sets" / f"{ranked_set_history.as_of.isoformat()}.json",
                ranked_set_history,
            )
        )

    written.append(_write_model(views_dir / "meta.json", build_meta(stores.meta, batch=batch)))
    return written


class _CachedLatestRunCandidates:
    """Serve one parsed latest run to every security-detail build.

    ``build_security_detail`` reads the latest run and its selections on every
    call, and the export loops over all candidates, so an uncached source would
    re-parse both once per ticker (quadratic in candidate count).
    """

    def __init__(self, inner: DbCandidatesSource) -> None:
        self._inner = inner
        self._loaded = False
        self._latest: CandidatesRun | None = None
        self._runs: dict[str, CandidatesRun | None] = {}
        self._selections: dict[str | None, list[dict[str, object]]] = {}

    def latest_run(self) -> CandidatesRun | None:
        if not self._loaded:
            self._latest = self._inner.latest_run()
            self._loaded = True
        return self._latest

    def previous_run(self) -> CandidatesRun | None:
        return self._inner.previous_run()

    def run(self, run_revision_id: str) -> CandidatesRun | None:
        if run_revision_id not in self._runs:
            self._runs[run_revision_id] = self._inner.run(run_revision_id)
        return self._runs[run_revision_id]

    def selections(self, *, run_revision_id: str | None = None) -> list[dict[str, object]]:
        if run_revision_id not in self._selections:
            self._selections[run_revision_id] = self._inner.selections(
                run_revision_id=run_revision_id
            )
        return self._selections[run_revision_id]


def _security_tickers(dashboard: DashboardView, screening: ScreeningView) -> list[str]:
    """Enumerate holdings, latest-run candidates, and shortlist tickers."""

    tickers = {holding.ticker for holding in dashboard.holdings}
    tickers.update(row.ticker for row in screening.rows)
    for shortlist in screening.shortlists:
        tickers.update(entry.ticker for entry in shortlist.entries)
    return sorted(tickers)


def _write_history(
    output_dir: Path,
    screening: ScreeningView,
    *,
    candidates: DbCandidatesSource,
    ledger: LedgerSource,
    research: ResearchSource,
    runs_db_path: Path,
) -> list[Path]:
    written: list[Path] = []
    history_dates = screening_run_asof_dates(runs_db_path)
    if not history_dates:
        _warn("no screening run is published; history/candidate-views skipped")
        return written
    for candidates_asof in history_dates:
        history = build_screening_history_run(
            candidates,
            ledger,
            research,
            as_of=candidates_asof,
        )
        if history is None:  # pragma: no cover - date index and exact lookup share one store
            continue
        written.append(
            _write_model(
                output_dir / "history/candidate-views" / f"{candidates_asof.isoformat()}.json",
                history,
            )
        )
    return written


def _ranked_set_history_record(candidates: DbCandidatesSource) -> _RankedSetHistoryRecord | None:
    """Freeze the latest run's ranked_set without applying the UI fallback run."""

    run = candidates.latest_run()
    if run is None:
        return None
    selections = candidates.selections(run_revision_id=run.run_revision_id)
    selection = max(
        selections,
        key=lambda item: (
            datetime.fromisoformat(str(item["created_at"])),
            str(item["selection_id"]),
        ),
        default=None,
    )
    members: list[_RankedSetHistoryMember] = []
    if selection is not None:
        payload = selection.get("payload")
        raw_ranked_set = payload.get("ranked_set") if isinstance(payload, dict) else None
        if raw_ranked_set is not None and (
            not isinstance(raw_ranked_set, list)
            or not all(isinstance(item, dict) for item in raw_ranked_set)
        ):
            raise ExportPreconditionError(
                "machine selection ranked_set must be an array of objects"
            )
        for item in raw_ranked_set or []:
            ticker = str(item.get("ticker", ""))
            if _TICKER_FORMAT.fullmatch(ticker) is None:
                raise ExportPreconditionError(
                    f"ranked_set ticker has an invalid format: {ticker!r}"
                )
            expected_return_pct = _history_number(item.get("expected_return_pct"))
            members.append(
                _RankedSetHistoryMember(
                    ticker=ticker,
                    rank=_history_rank(item.get("rank")),
                    er_annual=(None if expected_return_pct is None else expected_return_pct / 100),
                )
            )
    return _RankedSetHistoryRecord(
        as_of=run.asof_date,
        run_revision_id=run.run_revision_id,
        selection_status="selection_missing" if selection is None else "available",
        selection_id=None if selection is None else str(selection["selection_id"]),
        selection_created_at=(
            None if selection is None else datetime.fromisoformat(str(selection["created_at"]))
        ),
        members=members,
    )


def _history_rank(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ExportPreconditionError("ranked_set rank must be an integer or null")
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise ExportPreconditionError("ranked_set rank must be an integer or null") from error
    if parsed < 1:
        raise ExportPreconditionError("ranked_set rank must be positive")
    return parsed


def _history_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ExportPreconditionError("ranked_set expected return must be finite or null")
    try:
        parsed = float(str(value))
    except ValueError as error:
        raise ExportPreconditionError(
            "ranked_set expected return must be finite or null"
        ) from error
    if not math.isfinite(parsed):
        raise ExportPreconditionError("ranked_set expected return must be finite or null")
    return parsed


def _write_model(path: Path, model: BaseModel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(by_alias=True), encoding="utf-8")
    return path


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _root_error(root: Path) -> str | None:
    return repository_root_error(root, label="--repo-root")


def _batch_kind(value: str | None) -> MetaBatch | None:
    match value:
        case None:
            return None
        case "daily":
            return "daily"
        case "manual":
            return "manual"
    raise ValueError(f"unsupported batch kind: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_read_models",
        description="export serving read-model JSON (views/ + history/) from local stores",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch", choices=("daily", "manual"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    error = _root_error(root)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        reject_noncanonical_store_paths(root)
    except StoreLayoutError as legacy_error:
        print(f"error: {legacy_error}", file=sys.stderr)
        return 2
    output_dir = args.output_dir.resolve()
    try:
        written = export_read_models(root, output_dir, batch=_batch_kind(args.batch))
    except ExportPreconditionError as precondition:
        print(f"error: {precondition}", file=sys.stderr)
        return 1
    print(f"exported {len(written)} files under {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
