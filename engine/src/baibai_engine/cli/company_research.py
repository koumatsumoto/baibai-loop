"""部分調査の保存・exact読取・有限catalog。Reviewや提案へ自動昇格しない。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from baibai_engine.investment_judgment.company_research.context import AvailableResearch, Unreadable
from baibai_engine.investment_judgment.company_research.document import (
    ResearchDraft,
    scaffold_research,
)
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchConflictError,
    ResearchStoreService,
    load_research,
)
from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.filesystem import write_text_new
from baibai_engine.storage.sqlite.application.read import connect_read_only


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine investment-judgment company-research")
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("save", help="append a complete partial-research snapshot")
    save.add_argument("draft", type=Path)
    get = commands.add_parser("get", help="read one exact Research or reviewed Assessment")
    identity = get.add_mutually_exclusive_group(required=True)
    identity.add_argument("--research-id")
    identity.add_argument("--assessment-id")
    get.add_argument("--sha256", help="require this exact payload/core digest")
    latest = commands.add_parser(
        "latest", help="read separate saved Research and Assessment context"
    )
    latest.add_argument("--ticker", required=True)
    catalog = commands.add_parser(
        "catalog", help="browse saved companies in ticker order, not rank"
    )
    catalog.add_argument("--after-ticker")
    catalog.add_argument("--limit", type=int, default=20)
    scaffold = commands.add_parser(
        "scaffold", help="copy prior knowledge and original source dates"
    )
    scaffold.add_argument("--ticker", required=True)
    scaffold.add_argument("--research-id", help="new immutable identity (default: generated UUID)")
    scaffold.add_argument("--from-research-id", help="copy this exact prior instead of latest")
    scaffold.add_argument("--from-sha256")
    for command in (latest, catalog, scaffold):
        command.add_argument("--asof", required=True, type=date.fromisoformat)
        command.add_argument("--known-at", type=datetime.fromisoformat)
    for command in (save, get, latest, catalog, scaffold):
        command.add_argument("--db", type=Path)
        command.add_argument("--out", type=Path, help="create a new output file; never overwrite")
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    instant = now or datetime.now(JST)
    try:
        if args.out is not None and (args.out.exists() or args.out.is_symlink()):
            raise ResearchConflictError(f"output already exists: {args.out}")
        if args.command in {"latest", "catalog", "scaffold"} and args.known_at is not None:
            _datetime(args.known_at)
        payload: object
        match args.command:
            case "save":
                draft = ResearchDraft.model_validate(
                    safe_load(args.draft.read_text(encoding="utf-8"))
                )
                payload = (
                    ResearchStoreService(args.db, clock=lambda: instant)
                    .save(draft)
                    .model_dump(mode="json")
                )
            case "get":
                with closing(connect_read_only(args.db)) as connection:
                    if args.research_id is not None:
                        record = load_research(connection, args.research_id)
                        digest = record.reference.payload_sha256
                        payload = record.model_dump(mode="json")
                    else:
                        pair = load_reviewed_assessment(connection, args.assessment_id)
                        digest = pair.core_sha256
                        payload = {
                            "assessment_id": pair.assessment_id,
                            "core_sha256": digest,
                            "document": pair.document.model_dump(mode="json"),
                            "review": pair.review.model_dump(mode="json"),
                        }
                    if args.sha256 is not None and digest != args.sha256:
                        raise ValueError("exact reference digest mismatch")
            case "latest":
                ticker = normalize_ticker(args.ticker)
                payload = read_company_contexts(
                    args.db, tickers=(ticker,), as_of=args.asof, known_at=args.known_at or instant
                )[ticker].model_dump(mode="json")
            case "catalog":
                payload = research_catalog(
                    args.db,
                    as_of=args.asof,
                    known_at=args.known_at or instant,
                    after_ticker=args.after_ticker,
                    limit=args.limit,
                )
            case "scaffold":
                ticker = normalize_ticker(args.ticker)
                prior = None
                if args.from_research_id is not None:
                    with closing(connect_read_only(args.db)) as connection:
                        prior = load_research(connection, args.from_research_id)
                    if (
                        args.from_sha256 is not None
                        and prior.reference.payload_sha256 != args.from_sha256
                    ):
                        raise ValueError("exact prior digest mismatch")
                    if prior.recorded_at > (args.known_at or instant):
                        raise ValueError("prior Research is after input cutoff")
                else:
                    if args.from_sha256 is not None:
                        raise ValueError("--from-sha256 requires --from-research-id")
                    context = read_company_contexts(
                        args.db,
                        tickers=(ticker,),
                        as_of=args.asof,
                        known_at=args.known_at or instant,
                    )[ticker].research
                    if isinstance(context, Unreadable):
                        raise ValueError(context.reason)
                    if isinstance(context, AvailableResearch):
                        prior = context.record
                payload = scaffold_research(
                    research_id=args.research_id or f"company-research-{uuid4()}",
                    ticker=ticker,
                    as_of=args.asof,
                    prior=prior,
                )
            case _:
                raise AssertionError(f"unreachable command: {args.command}")
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.out is not None:
            try:
                write_text_new(args.out, content)
            except FileExistsError as error:
                raise ResearchConflictError(f"output already exists: {args.out}") from error
        sys.stdout.write(content)
        return 0
    except ResearchConflictError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except (ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
