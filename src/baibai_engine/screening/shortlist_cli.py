"""Explicit reviewed-shortlist publication command."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.run_store import ScreeningRunReader

from .shortlist import (
    ReviewedShortlist,
    ReviewedShortlistService,
    SelectionBinding,
    ShortlistConflictError,
)

_DISPOSITION_LABELS: Mapping[str, str] = {"rejected": "見送り"}


def publish_shortlist(
    draft_path: Path,
    *,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> int:
    try:
        raw = safe_load(draft_path.read_text(encoding="utf-8"))
        shortlist = ReviewedShortlist.model_validate(raw)
        reader = ScreeningRunReader(runs_db_path)
        selection = reader.get_selection(shortlist.selection_id)
        if selection is None:
            raise ShortlistConflictError(
                f"source selection is unavailable: {shortlist.selection_id}"
            )
        run = reader.get_run(selection.run_revision_id)
        if run is None:  # pragma: no cover - run-store FK invariant
            raise ShortlistConflictError(f"source run is unavailable: {selection.run_revision_id}")
        binding = SelectionBinding(
            selection_id=selection.selection_id,
            run_revision_id=selection.run_revision_id,
            as_of=date.fromisoformat(selection.as_of_date),
            profile=selection.profile,
            macro_context_id=selection.macro_context_id,
            candidate_tickers=frozenset(str(item["ticker"]) for item in run.candidates),
        )
        next_earnings_by_ticker = {
            str(item["ticker"]): item.get("next_earnings_date") for item in run.candidates
        }
        published = ReviewedShortlistService(app_db_path).publish(
            shortlist,
            selection=binding,
        )
    except (OSError, ValueError, ValidationError, ShortlistConflictError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(published.payload(), sys.stdout, sort_keys=False, allow_unicode=True)
    _print_reevaluation_task_suggestions(
        reevaluation_task_suggestions(published, next_earnings_by_ticker)
    )
    return 0


def reevaluation_task_suggestions(
    shortlist: ReviewedShortlist,
    next_earnings_by_ticker: Mapping[str, object | None],
) -> list[str]:
    """Build ready-to-run task-add lines for every non-selected entry.

    ``selected`` 以外の entry は「今は買わないが再評価する」判断であり、その dated
    trigger を task へ機械接続する。次回決算日が既知なら ``baibai-engine task add`` を
    そのまま実行できる形で、未公表なら手動で trigger 日を決める注記を返す。write は
    人間境界に残すので、この関数は提案文字列だけを組み立てる。title は narrative 散文
    を引かず ticker と disposition だけで組み、引用符事故を避ける。
    """
    suggestions: list[str] = []
    for entry in shortlist.entries:
        if entry.decision == "selected":
            continue
        disposition = _DISPOSITION_LABELS.get(entry.decision, entry.decision)
        earnings_date = _iso_date_or_none(next_earnings_by_ticker.get(entry.ticker))
        if earnings_date is None:
            suggestions.append(
                f"# {entry.ticker}（{disposition}）: 決算日未公表 — 手動で trigger 日を決めて "
                f"baibai-engine task add --kind follow-up --ticker {entry.ticker} "
                f'--title "{entry.ticker} 決算で{disposition}判断を再評価" '
                "--due <YYYY-MM-DD> を起票"
            )
            continue
        iso = earnings_date.isoformat()
        suggestions.append(
            f"baibai-engine task add --kind follow-up --ticker {entry.ticker} "
            f'--title "{entry.ticker} 決算で{disposition}判断を再評価" '
            f"--due {iso} --event-date {iso} "
            f'--event-label "{entry.ticker} 決算"'
        )
    return suggestions


def _print_reevaluation_task_suggestions(suggestions: list[str]) -> None:
    if not suggestions:
        return
    print(
        "再評価 trigger 提案（selected 以外 / 人間が確認して実行）:",
        file=sys.stderr,
    )
    for line in suggestions:
        print(line, file=sys.stderr)


def _iso_date_or_none(value: object | None) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


__all__ = ["publish_shortlist", "reevaluation_task_suggestions"]
