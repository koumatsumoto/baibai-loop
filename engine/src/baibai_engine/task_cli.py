"""Task CLI composition with optional market schedule reads outside the task domain."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import get_args

import yaml

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.primitives.time import JST
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import read_next_earnings_dates
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_engine.tasks.earnings_reconcile import reconcile_earnings_dates
from baibai_engine.tasks.models import TaskKind, TaskStatus
from baibai_engine.tasks.service import TaskService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine task")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add",
        help="add an operation task with a due date, trigger event, and close condition",
    )
    add.add_argument("--title", required=True)
    add.add_argument("--kind", required=True, choices=get_args(TaskKind))
    add.add_argument("--due", required=True, type=date.fromisoformat)
    add.add_argument("--ticker")
    add.add_argument("--event-date", type=date.fromisoformat)
    add.add_argument("--event-label")
    add.add_argument("--body")
    add.add_argument("--related-ref", action="append", default=[])
    list_parser = commands.add_parser(
        "list",
        help="list tasks in due order, optionally filtered by status",
    )
    list_parser.add_argument("--status", choices=get_args(TaskStatus))
    # 終端の 2 値は「判断が済んだ」と「問いが不成立になった」で、閉じた理由が違う。
    close_help = {
        "done": "close a task whose judgment and canonical update are complete",
        "drop": "close a task whose question no longer holds",
    }
    for command, help_text in close_help.items():
        close = commands.add_parser(command, help=help_text)
        close.add_argument("task_id")
    reconcile = commands.add_parser(
        "reconcile-earnings",
        help="compare earnings-related tasks with the disclosed earnings calendar",
    )
    reconcile.add_argument("--sqlite-path", type=Path, default=MARKET_DB_PATH)
    edit = commands.add_parser(
        "edit",
        help="change a task's title, kind, due date, trigger event, body, or refs",
    )
    edit.add_argument("task_id")
    edit.add_argument("--title")
    edit.add_argument("--kind", choices=get_args(TaskKind))
    edit.add_argument("--due", type=date.fromisoformat)
    edit.add_argument("--ticker")
    edit.add_argument("--clear-ticker", action="store_true")
    edit.add_argument("--event-date", type=date.fromisoformat)
    edit.add_argument("--event-label")
    edit.add_argument("--clear-event", action="store_true")
    edit.add_argument("--body")
    edit.add_argument("--clear-body", action="store_true")
    edit.add_argument("--related-ref", action="append")
    edit.add_argument("--clear-related-refs", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = TaskService(args.db)
    current_date = today or datetime.now(JST).date()
    try:
        if args.command == "add":
            task = service.add(
                title=args.title,
                kind=args.kind,
                due_date=args.due,
                created_at=current_date,
                ticker=args.ticker,
                event_date=args.event_date,
                event_label=args.event_label,
                body_md=args.body,
                related_refs=tuple(args.related_ref),
            )
            _emit(task.payload())
        elif args.command == "list":
            _emit({"tasks": [task.payload() for task in service.list(status=args.status)]})
        elif args.command in {"done", "drop"}:
            if args.command == "done":
                task = service.close(args.task_id, status="done", closed_at=current_date)
            else:
                task = service.close(args.task_id, status="dropped", closed_at=current_date)
            _emit(task.payload())
        elif args.command == "reconcile-earnings":
            return _reconcile_earnings(service, args, today=current_date)
        elif args.command == "edit":
            _emit(service.edit(args.task_id, _edit_changes(args)).payload())
        else:  # pragma: no cover
            raise AssertionError(f"unreachable task command: {args.command}")
    except (OSError, ValueError, sqlite3.Error, JPXProviderError, SQLiteSchemaError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _reconcile_earnings(service: TaskService, args: argparse.Namespace, *, today: date) -> int:
    """Report where open task dates stand against the published schedule.

    This never writes. Both published dates and estimates can change. The comparison
    names the difference; an explicit `task edit` applies a reviewed change under
    the task 規約 (jobs/OPERATIONS.md).
    """
    published = read_next_earnings_dates(args.sqlite_path, today)
    if published is None:
        print(
            f"error: current earnings schedule unavailable in {args.sqlite_path}",
            file=sys.stderr,
        )
        return 1
    results = reconcile_earnings_dates(service.list(status="open"), published, today=today)
    # One flat line per non-confirming task, prefixed so a log filter can keep the
    # findings without the confirmations. The command's whole product is this
    # report; a batch that swallows it runs the step for nothing.
    for result in results:
        if result.outcome != "confirm":
            drift = "" if result.drift_days is None else f" drift={result.drift_days:+d}d"
            print(
                f"reconcile-earnings\t{result.outcome}\t{result.ticker}\t"
                f"{result.current_event_date} -> {result.published_event_date}{drift}",
                file=sys.stdout,
            )
    _emit(
        {
            "schedule_window_end": max(published.values()).isoformat() if published else None,
            "reconciliations": [
                {
                    "task_id": result.task_id,
                    "ticker": result.ticker,
                    "current_event_date": (
                        result.current_event_date.isoformat()
                        if result.current_event_date is not None
                        else None
                    ),
                    "published_event_date": result.published_event_date.isoformat(),
                    "drift_days": result.drift_days,
                    "outcome": result.outcome,
                }
                for result in results
            ],
        }
    )
    return 0


def _edit_changes(args: argparse.Namespace) -> dict[str, object]:
    changes: dict[str, object] = {}
    for argument, field in (
        ("title", "title"),
        ("kind", "kind"),
        ("due", "due_date"),
        ("ticker", "ticker"),
        ("event_date", "event_date"),
        ("event_label", "event_label"),
        ("body", "body_md"),
    ):
        value = getattr(args, argument)
        if value is not None:
            changes[field] = value
    if args.clear_ticker:
        changes["ticker"] = None
    if args.clear_event:
        changes["event_date"] = None
        changes["event_label"] = None
    if args.clear_body:
        changes["body_md"] = None
    if args.related_ref is not None:
        changes["related_refs"] = tuple(args.related_ref)
    if args.clear_related_refs:
        changes["related_refs"] = ()
    return changes


def _emit(payload: object) -> None:
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)
