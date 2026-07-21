"""Export the serving read-model JSON tree (views/ + history/) from local stores.

Runs the same ``baibai_app.readmodel`` builders as the local FastAPI app, so each
``views/*.json`` file carries the same JSON shape as the corresponding ``/api``
response. The output directory is what a scheduled batch uploads to the serving
object store; no business logic exists beyond these builders.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from baibai_app.readmodel.builders import (
    build_dashboard,
    build_macro,
    build_meta,
    build_program_state,
    build_screening,
    build_security_detail,
)
from baibai_app.readmodel.models import DashboardView, MetaBatch, ScreeningView
from baibai_app.sources.db_sources import (
    DbCandidatesSource,
    DbLedgerSource,
    DbMacroSource,
    DbMarketPriceSource,
    DbMetaSource,
    DbProgramSource,
    DbResearchSource,
    DbTaskSource,
    load_macro_dashboard_config,
)
from baibai_app.sources.types import CandidatesRun
from baibai_engine.read_api import screening_run_payload

_JST = ZoneInfo("Asia/Tokyo")
_MACRO_PERIODS = ("1y", "5y", "10y", "max")
_MACRO_GRANULARITIES = ("daily", "weekly", "monthly", "yearly")


@dataclass(frozen=True, slots=True)
class _Stores:
    ledger: DbLedgerSource
    research: DbResearchSource
    tasks: DbTaskSource
    candidates: DbCandidatesSource
    macro: DbMacroSource
    program: DbProgramSource
    market: DbMarketPriceSource
    meta: DbMetaSource
    runs_db_path: Path


def _build_stores(root: Path) -> _Stores:
    db_path = (root / "data/app/baibai.sqlite").resolve()
    runs_db_path = (root / "data/screening/runs.sqlite").resolve()
    indicators_db_path = (root / "data/indicators/macro.sqlite").resolve()
    return _Stores(
        ledger=DbLedgerSource(db_path),
        research=DbResearchSource(db_path),
        tasks=DbTaskSource(db_path),
        candidates=DbCandidatesSource(runs_db_path, db_path),
        macro=DbMacroSource(
            db_path,
            indicators_db_path,
            load_macro_dashboard_config(root / "records/_config/macro-dashboard.yaml"),
        ),
        program=DbProgramSource(db_path),
        market=DbMarketPriceSource(root / "data/screening/market.sqlite"),
        meta=DbMetaSource(db_path, runs_db_path, indicators_db_path),
        runs_db_path=runs_db_path,
    )


def export_read_models(
    root: Path,
    output_dir: Path,
    *,
    batch: MetaBatch | None = None,
) -> list[Path]:
    """Write every serving JSON under ``output_dir`` and return the written paths."""

    stores = _build_stores(root)
    views_dir = output_dir / "views"
    written: list[Path] = []

    dashboard = build_dashboard(
        stores.ledger,
        stores.research,
        stores.tasks,
        stores.candidates,
        stores.market,
        macro=stores.macro,
    )
    written.append(_write_model(views_dir / "dashboard.json", dashboard))

    screening = build_screening(stores.candidates, stores.ledger, stores.research)
    written.append(_write_model(views_dir / "screening_latest.json", screening))

    written.append(_write_model(views_dir / "program.json", build_program_state(stores.program)))

    as_of = datetime.now(_JST).date()
    for period in _MACRO_PERIODS:
        for granularity in _MACRO_GRANULARITIES:
            macro = build_macro(stores.macro, as_of=as_of, period=period, granularity=granularity)
            written.append(_write_model(views_dir / f"macro--{period}-{granularity}.json", macro))

    written.append(_write_model(views_dir / "meta.json", build_meta(stores.meta, batch=batch)))

    cached_candidates = _CachedLatestRunCandidates(stores.candidates)
    for ticker in _security_tickers(dashboard, screening):
        detail = build_security_detail(
            ticker,
            stores.ledger,
            stores.research,
            cached_candidates,
            stores.market,
        )
        if detail is None:
            _warn(f"security view is unavailable for ticker {ticker}; skipped")
            continue
        written.append(_write_model(views_dir / f"security--{ticker}.json", detail))

    written.extend(_write_history(output_dir, screening, runs_db_path=stores.runs_db_path))
    return written


class _CachedLatestRunCandidates:
    """Serve one parsed latest run to every security-detail build.

    ``build_security_detail`` reads the latest run on every call, and the export
    loops over the full candidate pool, so an uncached source would re-parse the
    pool once per ticker (quadratic in pool size).
    """

    def __init__(self, inner: DbCandidatesSource) -> None:
        self._inner = inner
        self._loaded = False
        self._latest: CandidatesRun | None = None

    def latest_run(self) -> CandidatesRun | None:
        if not self._loaded:
            self._latest = self._inner.latest_run()
            self._loaded = True
        return self._latest


def _security_tickers(dashboard: DashboardView, screening: ScreeningView) -> list[str]:
    """Enumerate holdings, latest-run candidates, and reviewed-shortlist tickers."""

    tickers = {holding.ticker for holding in dashboard.holdings}
    tickers.update(row.ticker for row in screening.rows)
    for shortlist in screening.reviewed_shortlists:
        tickers.update(entry.ticker for entry in shortlist.entries)
    return sorted(tickers)


def _write_history(
    output_dir: Path,
    screening: ScreeningView,
    *,
    runs_db_path: Path,
) -> list[Path]:
    written: list[Path] = []
    if screening.run is not None and screening.selections:
        latest = max(screening.selections, key=lambda item: item.created_at)
        asof = screening.run.asof_date.isoformat()
        written.append(_write_model(output_dir / "history/select" / f"{asof}.json", latest))
    else:
        _warn("no machine selection is published; history/select skipped")
    raw_run = screening_run_payload(runs_db_path)
    if raw_run is not None:
        path = output_dir / "history/candidate-pool" / f"{raw_run['as_of_date']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw_run, ensure_ascii=False), encoding="utf-8")
        written.append(path)
    else:
        _warn("no screening run is published; history/candidate-pool skipped")
    return written


def _write_model(path: Path, model: BaseModel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(by_alias=True), encoding="utf-8")
    return path


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _root_error(root: Path) -> str | None:
    if not (root / "pyproject.toml").is_file():
        return f"--repo-root does not contain pyproject.toml: {root}"
    if not (root / "records").is_dir():
        return f"--repo-root does not contain records/: {root}"
    return None


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
    output_dir = args.output_dir.resolve()
    written = export_read_models(root, output_dir, batch=_batch_kind(args.batch))
    print(f"exported {len(written)} files under {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
