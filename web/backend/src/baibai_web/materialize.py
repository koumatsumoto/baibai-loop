"""Export the serving read-model JSON tree (views/ + history/) from local stores.

Runs the same ``baibai_web.readmodel`` builders as the local FastAPI app, so each
``views/*.json`` file carries the same JSON shape as the corresponding ``/api``
response. The output directory is what a scheduled batch uploads to the serving
object store; no business logic exists beyond these builders.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import replace
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from baibai_engine.read_api import (
    INDICATORS_DB_PATH,
    READING_RULES_PATH,
    MaterializationPreconditionError,
    PortfolioSnapshot,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
    screening_run_asof_dates,
    validate_market_store_hydration,
    validate_reading_rules,
)
from baibai_web.readmodel.assessment import build_market_context_assessment_detail
from baibai_web.readmodel.builders import (
    build_daily_delta,
    build_dashboard,
    build_meta,
    build_operations_view,
    build_tasks,
)
from baibai_web.readmodel.market_context import build_indicator_series, build_market_context
from baibai_web.readmodel.models import DashboardView, MetaBatch, ScreeningView
from baibai_web.readmodel.stocks import (
    build_screening,
    build_screening_history_run,
    build_security_detail,
    prepare_security_inputs,
)
from baibai_web.readmodel.stocks.research import (
    build_proposal_detail,
    build_proposal_list,
    build_proposal_preview,
    build_stock_assessment_detail,
    build_stock_assessment_list,
)
from baibai_web.sources.db_sources import (
    DbLedgerSource,
    DbMetaSource,
    DbResearchSource,
    DbScreeningSource,
)
from baibai_web.sources.factory import build_sources
from baibai_web.sources.protocols import LedgerSource, ResearchSource
from baibai_web.sources.types import ResearchRevision, ScreeningRunRecord

_JST = ZoneInfo("Asia/Tokyo")
# Same shape the ledger and run store enforce at write time; re-checked here so a
# store-derived string never reaches filename composition unvalidated.
_TICKER_FORMAT = re.compile(r"[0-9A-Z]{4}")
# assessment context_id charset; excludes path separators so it is safe in a filename
# and mirrors the Worker route validation for the same key.
_MARKET_CONTEXT_ASSESSMENT_ID_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_INDICATOR_SERIES_ID_FORMAT = re.compile(r"[a-z0-9._-]{1,128}")
_ASSESSMENT_ID_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SECURITY_BATCH_SIZE = 64


ExportPreconditionError = MaterializationPreconditionError


def export_read_models(
    root: Path,
    output_dir: Path,
    *,
    batch: MetaBatch | None = None,
    include_history: bool = True,
) -> list[Path]:
    """Write every serving JSON under ``output_dir`` and return the written paths.

    ``views/`` is recreated from scratch so it always carries the complete image of
    one export and no stale per-ticker view survives a shrinking target set;
    Retained ``history/`` dates are regenerated from past runs with current
    holding, reservation and Research annotations. ``views/meta.json`` is written last,
    after every other file has been written successfully.
    """

    stores = build_sources(root)
    validate_reading_rules(root / READING_RULES_PATH)
    validate_market_store_hydration(stores.market_db_path)
    # These sources live only for this export. API requests keep the fresh DB sources.
    screening_source = _CachedScreeningSource(stores.runs_db_path)
    stores = replace(
        stores,
        ledger=_CachedLedgerSource(stores.app_db_path, stores.market_db_path),
        research=_CachedResearchSource(stores.app_db_path),
        screening=screening_source,
        meta=DbMetaSource(stores.app_db_path, screening_source, root / INDICATORS_DB_PATH),
    )
    views_dir = output_dir / "views"
    if views_dir.is_dir():
        shutil.rmtree(views_dir)
    views_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    dashboard = build_dashboard(
        stores.ledger,
        stores.research,
        stores.screening,
        stores.market,
    )
    written.append(_write_model(views_dir / "dashboard.json", dashboard))

    written.append(
        _write_model(
            views_dir / "tasks.json",
            build_tasks(
                stores.tasks,
                stores.ledger,
                stores.research,
                stores.screening,
                stores.market,
            ),
        )
    )

    prepared_security = prepare_security_inputs(stores.ledger, stores.research, stores.screening)
    stores.market.prepare_market_context(as_of=prepared_security.today)
    screening = build_screening(
        stores.screening,
        stores.ledger,
        stores.research,
        stores.er_level_calibration,
        external_assessment=stores.external_assessment,
        market=stores.market,
        external_cutoff=prepared_security.external_cutoff,
    )
    written.append(_write_model(views_dir / "screening_latest.json", screening))

    written.append(
        _write_model(
            views_dir / "daily-delta.json",
            build_daily_delta(
                stores.screening,
                stores.ledger,
                stores.research,
                stores.market,
            ),
        )
    )

    written.append(
        _write_model(views_dir / "operations.json", build_operations_view(stores.operations))
    )

    written.append(
        _write_model(
            views_dir / "stock-assessments.json",
            build_stock_assessment_list(stores.research),
        )
    )
    written.append(
        _write_model(views_dir / "investment-proposals.json", build_proposal_list(stores.research))
    )
    known_at = datetime.now(_JST)
    as_of = prepared_security.today
    market_context = build_market_context(
        stores.market_context, as_of=as_of, known_at=known_at, market=stores.market
    )
    written.append(_write_model(views_dir / "market-context.json", market_context))
    for group in stores.market_context.groups:
        for configured in group.series:
            series_id = configured.series_id
            if _INDICATOR_SERIES_ID_FORMAT.fullmatch(series_id) is None:
                _warn(f"indicator series_id has an unexpected format: {series_id!r}; view skipped")
                continue
            series = build_indicator_series(stores.market_context, series_id=series_id, as_of=as_of)
            if series is None:  # pragma: no cover - group entries come from the registry
                _warn(f"indicator series view is unavailable for {series_id!r}; skipped")
                continue
            written.append(_write_model(views_dir / f"indicator-series--{series_id}.json", series))

    for context in stores.market_context.assessment_history():
        context_id = str(context["context_id"])
        if _MARKET_CONTEXT_ASSESSMENT_ID_FORMAT.fullmatch(context_id) is None:
            raise ValueError(
                f"indexed assessment context_id has an unexpected format: {context_id!r}"
            )
        # Exact-ID history is independent of the current analytical cutoff. Every
        # indexed publication must remain readable for this export to be complete.
        detail = build_market_context_assessment_detail(
            stores.market_context, context_id=context_id, as_of=as_of
        )
        written.append(
            _write_model(views_dir / f"market-context-assessment--{context_id}.json", detail)
        )

    # Immutable exact publications remain addressable after the current analysis or
    # assessment revision changes. Current calculations get their own timestamped view.
    for revision in stores.research.revisions():
        assessment_id = revision.assessment_id
        if _ASSESSMENT_ID_FORMAT.fullmatch(assessment_id) is None:
            raise ValueError(f"assessment_id has an unexpected format: {assessment_id!r}")
        stock_detail = build_stock_assessment_detail(stores.research, assessment_id=assessment_id)
        if stock_detail is None:
            raise ValueError(f"indexed Stock Assessment is unavailable: {assessment_id}")
        written.append(
            _write_model(views_dir / f"stock-assessment--{assessment_id}.json", stock_detail)
        )

    for publication in stores.research.proposals():
        proposal_id = str(publication["proposal_id"])
        if _ASSESSMENT_ID_FORMAT.fullmatch(proposal_id) is None:
            raise ValueError(f"proposal_id has an unexpected format: {proposal_id!r}")
        proposal = build_proposal_detail(stores.research, proposal_id=proposal_id)
        if proposal is None:
            raise ValueError(f"indexed Trade Proposal is unavailable: {proposal_id}")
        written.append(
            _write_model(views_dir / f"investment-proposal--{proposal_id}.json", proposal)
        )
        preview = build_proposal_preview(
            stores.research,
            proposal_id=proposal_id,
            sqlite_path=stores.market_db_path,
            now=known_at,
        )
        if preview is None:
            raise ValueError(f"indexed Trade Proposal preview is unavailable: {proposal_id}")
        written.append(
            _write_model(views_dir / f"investment-proposal-preview--{proposal_id}.json", preview)
        )

    tickers = sorted(
        set(_security_tickers(dashboard, screening))
        | {item.ticker for item in screening.external_assessment_securities}
        | set(prepared_security.revisions)
        | set(prepared_security.proposals)
    )
    valid_tickers = []
    for ticker in tickers:
        if _TICKER_FORMAT.fullmatch(ticker) is None:
            _warn(f"ticker has an unexpected format: {ticker!r}; security view skipped")
            continue
        valid_tickers.append(ticker)
    for offset in range(0, len(valid_tickers), _SECURITY_BATCH_SIZE):
        batch_tickers = valid_tickers[offset : offset + _SECURITY_BATCH_SIZE]
        stores.market.prepare_security_market_contexts(batch_tickers, as_of=prepared_security.today)
        for ticker in batch_tickers:
            security = build_security_detail(
                ticker,
                stores.ledger,
                stores.research,
                stores.screening,
                stores.market,
                prepared=prepared_security,
                external_assessment=stores.external_assessment,
            )
            if security is None:
                _warn(f"security view is unavailable for ticker {ticker}; skipped")
                continue
            written.append(_write_model(views_dir / f"security--{ticker}.json", security))

    if include_history:
        written.extend(
            _write_history(
                output_dir,
                screening_source=stores.screening,
                ledger=stores.ledger,
                research=stores.research,
                runs_db_path=stores.runs_db_path,
            )
        )
    written.append(_write_model(views_dir / "meta.json", build_meta(stores.meta, batch=batch)))
    return written


class _CachedScreeningSource(DbScreeningSource):
    @cached_property
    def _latest_run(self) -> ScreeningRunRecord | None:
        return super().latest_run()

    def latest_run(self) -> ScreeningRunRecord | None:
        return self._latest_run

    def run_as_of(self, as_of: date) -> ScreeningRunRecord | None:
        latest = self.latest_run()
        if latest is not None and latest.as_of == as_of:
            return latest
        return super().run_as_of(as_of)


class _CachedLedgerSource(DbLedgerSource):
    @cached_property
    def _exists(self) -> bool:
        return super().exists()

    @cached_property
    def _snapshot(self) -> PortfolioSnapshot:
        return super().snapshot()

    def exists(self) -> bool:
        return self._exists

    def snapshot(self) -> PortfolioSnapshot:
        return self._snapshot


class _CachedResearchSource(DbResearchSource):
    @cached_property
    def _revisions(self) -> list[ResearchRevision]:
        return super().revisions()

    def revisions(self) -> list[ResearchRevision]:
        return self._revisions

    @cached_property
    def _proposals(self) -> list[dict[str, object]]:
        return super().proposals()

    def proposals(self) -> list[dict[str, object]]:
        return self._proposals


def _security_tickers(dashboard: DashboardView, screening: ScreeningView) -> list[str]:
    """Enumerate portfolio tickers and latest-run analyses; Research is added by caller."""

    tickers = {holding.ticker for holding in dashboard.holdings}
    tickers.update(item.ticker for item in dashboard.reservations)
    tickers.update(row.ticker for row in screening.stock_analyses)
    return sorted(tickers)


def _write_history(
    output_dir: Path,
    *,
    screening_source: DbScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    runs_db_path: Path,
) -> list[Path]:
    written: list[Path] = []
    history_dates = screening_run_asof_dates(runs_db_path)
    if not history_dates:
        _warn("no screening run is published; history/candidate-views skipped")
        return written
    for screening_as_of in history_dates:
        history = build_screening_history_run(
            screening_source,
            ledger,
            research,
            as_of=screening_as_of,
        )
        if history is None:
            raise ValueError(f"indexed Screening history is unavailable: {screening_as_of}")
        written.append(
            _write_model(
                output_dir / "history/candidate-views" / f"{screening_as_of.isoformat()}.json",
                history,
            )
        )
    return written


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
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="preserve retained Screening history when it did not update",
    )
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
    except StoreLayoutError as layout_error:
        print(f"error: {layout_error}", file=sys.stderr)
        return 2
    output_dir = args.output_dir.resolve()
    try:
        written = export_read_models(
            root, output_dir, batch=_batch_kind(args.batch), include_history=not args.skip_history
        )
    except ExportPreconditionError as precondition:
        print(f"error: {precondition}", file=sys.stderr)
        return 1
    print(f"exported {len(written)} files under {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
