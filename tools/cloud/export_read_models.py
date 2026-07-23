"""Export the serving read-model JSON tree (views/ + history/) from local stores.

Runs the same ``baibai_app.readmodel`` builders as the local FastAPI app, so each
``views/*.json`` file carries the same JSON shape as the corresponding ``/api``
response. The output directory is what a scheduled batch uploads to the serving
object store; no business logic exists beyond these builders.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from baibai_app.readmodel.builders import (
    build_dashboard,
    build_macro,
    build_macro_context_detail,
    build_meta,
    build_operations_view,
    build_screening,
    build_security_detail,
)
from baibai_app.readmodel.models import DashboardView, MetaBatch, ScreeningView
from baibai_app.sources.db_sources import DbCandidatesSource
from baibai_app.sources.factory import build_sources
from baibai_app.sources.types import CandidatesRun
from baibai_engine.read_api import screening_run_payload

_JST = ZoneInfo("Asia/Tokyo")
_MACRO_PERIODS = ("1y", "5y", "10y", "max")
_MACRO_GRANULARITIES = ("daily", "weekly", "monthly", "yearly")
# Same shape the ledger and run store enforce at write time; re-checked here so a
# store-derived string never reaches filename composition unvalidated.
_TICKER_FORMAT = re.compile(r"[0-9A-Z]{4}")
# macro context_id charset; excludes path separators so it is safe in a filename
# and mirrors the Worker route validation for the same key.
_MACRO_CONTEXT_ID_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,128}")


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
        macro=stores.macro,
    )
    written.append(_write_model(views_dir / "dashboard.json", dashboard))

    screening = build_screening(stores.candidates, stores.ledger, stores.research)
    written.append(_write_model(views_dir / "screening_latest.json", screening))

    written.append(
        _write_model(views_dir / "operations.json", build_operations_view(stores.operations))
    )

    as_of = datetime.now(_JST).date()
    for period in _MACRO_PERIODS:
        for granularity in _MACRO_GRANULARITIES:
            macro = build_macro(stores.macro, as_of=as_of, period=period, granularity=granularity)
            written.append(_write_model(views_dir / f"macro--{period}-{granularity}.json", macro))

    for context in stores.macro.contexts():
        context_id = str(context["context_id"])
        if _MACRO_CONTEXT_ID_FORMAT.fullmatch(context_id) is None:
            _warn(f"macro context_id has an unexpected format: {context_id!r}; report view skipped")
            continue
        detail = build_macro_context_detail(stores.macro, context_id=context_id, as_of=as_of)
        written.append(_write_model(views_dir / f"macro-context--{context_id}.json", detail))

    cached_candidates = _CachedLatestRunCandidates(stores.candidates)
    for ticker in _security_tickers(dashboard, screening):
        if _TICKER_FORMAT.fullmatch(ticker) is None:
            _warn(f"ticker has an unexpected format: {ticker!r}; security view skipped")
            continue
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

    written.append(_write_model(views_dir / "meta.json", build_meta(stores.meta, batch=batch)))
    return written


class _CachedLatestRunCandidates:
    """Serve one parsed latest run to every security-detail build.

    ``build_security_detail`` reads the latest run on every call, and the export
    loops over all candidates, so an uncached source would re-parse the run once
    per ticker (quadratic in candidate count).
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
    if raw_run is None:
        _warn("no screening run is published; history/candidates skipped")
        return written
    try:
        candidates_asof = date.fromisoformat(str(raw_run["as_of_date"]))
    except ValueError:
        _warn(
            f"run as_of_date has an unexpected format: {raw_run['as_of_date']!r}; "
            "history/candidates skipped"
        )
        return written
    path = output_dir / "history/candidates" / f"{candidates_asof.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw_run, ensure_ascii=False), encoding="utf-8")
    written.append(path)
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
    if not (root / "method").is_dir():
        return f"--repo-root does not contain method/: {root}"
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
