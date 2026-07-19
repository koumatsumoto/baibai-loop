"""Task writer CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST

from .models import TaskKind, TaskStatus
from .service import TaskConflictError, TaskNotFoundError, TaskService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine task")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--kind", required=True, choices=_kinds())
    add.add_argument("--due", required=True, type=date.fromisoformat)
    add.add_argument("--ticker")
    add.add_argument("--event-date", type=date.fromisoformat)
    add.add_argument("--event-label")
    add.add_argument("--body")
    add.add_argument("--related-ref", action="append", default=[])
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--status", choices=_statuses())
    for command in ("done", "drop"):
        close = commands.add_parser(command)
        close.add_argument("task_id")
    edit = commands.add_parser("edit")
    edit.add_argument("task_id")
    edit.add_argument("--title")
    edit.add_argument("--kind", choices=_kinds())
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
        elif args.command == "edit":
            _emit(service.edit(args.task_id, _edit_changes(args)).payload())
        else:  # pragma: no cover
            raise AssertionError(f"unreachable task command: {args.command}")
    except (OSError, ValueError, ValidationError, TaskConflictError, TaskNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
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


def _kinds() -> tuple[TaskKind, ...]:
    return ("earnings-review", "ops", "follow-up", "other")


def _statuses() -> tuple[TaskStatus, ...]:
    return ("open", "done", "dropped")
