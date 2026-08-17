"""Explicit shortlist publication command."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.tasks.service import TaskService

from .shortlist import (
    SelectionBinding,
    Shortlist,
    ShortlistConflictError,
    ShortlistService,
)

_DISPOSITION_LABELS: Mapping[str, str] = {"rejected": "見送り"}


def _machine_estimates(candidates: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Index the run's machine E[r] by ticker so publish can burn it into the judgment."""

    estimates: dict[str, float] = {}
    for candidate in candidates:
        metrics = candidate.get("metrics")
        value = metrics.get("er_annual") if isinstance(metrics, Mapping) else None
        ticker = candidate.get("ticker")
        if ticker is None or not isinstance(value, int | float) or isinstance(value, bool):
            continue
        estimates[str(ticker)] = float(value)
    return estimates


def _machine_rows(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """Index the selection's longlist rows by ticker.

    The longlist is the ranked set the human reviewed, so its row is what the
    judgment was made against. A selection published without `--longlist-top` has
    nothing to burn in and leaves the entries without a snapshot.
    """

    longlist = payload.get("longlist")
    if not isinstance(longlist, Sequence) or isinstance(longlist, str | bytes):
        return {}
    rows: dict[str, Mapping[str, object]] = {}
    for item in longlist:
        if not isinstance(item, Mapping):
            continue
        ticker = item.get("ticker")
        if ticker is not None:
            rows[str(ticker)] = item
    return rows


def publish_shortlist(
    draft_path: Path,
    *,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> int:
    try:
        raw = safe_load(draft_path.read_text(encoding="utf-8"))
        shortlist = Shortlist.model_validate(raw)
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
            candidate_er=_machine_estimates(run.candidates),
            candidate_machine_rows=_machine_rows(selection.payload),
        )
        earnings_by_ticker = _earnings_by_ticker(run.candidates)
        published = ShortlistService(app_db_path).publish(
            shortlist,
            selection=binding,
        )
    except (OSError, ValueError, ValidationError, ShortlistConflictError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(published.payload(), sys.stdout, sort_keys=False, allow_unicode=True)
    _print_reevaluation_task_suggestions(
        reevaluation_task_suggestions(
            published,
            earnings_by_ticker,
            existing_followups=_open_followup_events(app_db_path),
        )
    )
    return 0


def _open_followup_events(app_db_path: Path | None) -> set[tuple[str, str]]:
    """Which ``(ticker, event date)`` pairs already carry an open follow-up.

    Read here rather than inside the suggestion builder so that stays a pure function of
    the shortlist and the run. A store that cannot be read yields no pairs: the
    suggestions are advisory lines a human runs, so losing the de-duplication is worth
    less than failing a publication that has already been written.
    """

    try:
        tasks = TaskService(app_db_path).list(status="open")
    except (OSError, sqlite3.Error, ValueError):  # pragma: no cover - advisory read
        return set()
    return {
        (task.ticker, task.event_date.isoformat())
        for task in tasks
        if task.kind == "follow-up" and task.ticker is not None and task.event_date is not None
    }


def reevaluation_task_suggestions(
    shortlist: Shortlist,
    earnings_by_ticker: Mapping[str, Mapping[str, object | None]],
    *,
    existing_followups: Collection[tuple[str, str]] = (),
) -> list[str]:
    """Build ready-to-run task-add lines for every non-selected entry.

    ``selected`` 以外の entry は「今は買わないが再評価する」判断であり、その dated
    trigger を task へ機械接続する。開示済みまたは publish 時点より過去の event は捨て、
    次の公表日、将来の推定日、undated condition の順で進める。write は人間境界に残すので、
    この関数は提案文字列だけを組み立てる。title は narrative 散文を引かず ticker と
    disposition だけで組み、引用符事故を避ける。

    ``existing_followups`` は既に open な follow-up の ``(ticker, event 日付)``。同じ
    銘柄の同じ event へ 2 度目を提案すると、既存 task が持つ具体的な確認事項を、汎用の
    title を持つ新しい行が隣に並べて薄める。cycle をまたいで同じ決算が trigger になる
    のは正常なので、提案の代わりに既存があることを出して、人間が既存側を更新できるように
    する。
    """
    already = {(str(ticker), str(day)) for ticker, day in existing_followups}
    suggestions: list[str] = []
    for entry in shortlist.entries:
        if entry.decision == "selected":
            continue
        disposition = _DISPOSITION_LABELS.get(entry.decision, entry.decision)
        trigger = earnings_by_ticker.get(entry.ticker, {})
        announced = _iso_date_or_none(trigger.get("next_earnings_date"))
        estimated = _iso_date_or_none(trigger.get("next_earnings_estimated_date"))
        disclosed = _iso_date_or_none(trigger.get("fin_latest_disclosed_date"))
        earliest_due = max(shortlist.as_of, shortlist.published_at.astimezone(JST).date())
        earnings_date = _usable_future_event(
            announced,
            disclosed=disclosed,
            earliest_due=earliest_due,
        )
        event_label = "決算"
        if earnings_date is None:
            earnings_date = _usable_future_event(
                estimated,
                disclosed=disclosed,
                earliest_due=earliest_due,
            )
            event_label = "決算（推定）"
        if earnings_date is None:
            suggestions.append(
                f"# {entry.ticker}（{disposition}）: 将来の決算日なし — "
                "次回決算日の公表または新規 material 開示で再評価"
            )
            continue
        iso = earnings_date.isoformat()
        if (entry.ticker, iso) in already:
            suggestions.append(
                f"# {entry.ticker}（{disposition}）: {iso} の follow-up は既に open — "
                "重複起票せず既存 task を task edit で更新する"
            )
            continue
        suggestions.append(
            f"baibai-engine task add --kind follow-up --ticker {entry.ticker} "
            f'--title "{entry.ticker} 決算で{disposition}判断を再評価" '
            f"--due {iso} --event-date {iso} "
            f'--event-label "{entry.ticker} {event_label}"'
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


def _earnings_by_ticker(
    candidates: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object | None]]:
    result: dict[str, Mapping[str, object | None]] = {}
    for item in candidates:
        metrics = item.get("metrics")
        metric_values = metrics if isinstance(metrics, Mapping) else {}
        result[str(item["ticker"])] = {
            "next_earnings_date": item.get("next_earnings_date"),
            "next_earnings_estimated_date": metric_values.get("next_earnings_estimated_date"),
            "fin_latest_disclosed_date": metric_values.get("fin_latest_disclosed_date"),
        }
    return result


def _iso_date_or_none(value: object | None) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _usable_future_event(
    event_date: date | None,
    *,
    disclosed: date | None,
    earliest_due: date,
) -> date | None:
    if event_date is None or event_date < earliest_due:
        return None
    if disclosed is not None and disclosed >= event_date:
        return None
    return event_date


__all__ = ["publish_shortlist", "reevaluation_task_suggestions"]
