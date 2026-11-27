"""Review USD research or save and read a non-executable decision in an isolated App."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Never

from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReportReview,
    ReviewedReport,
    USProposalDraft,
    USProposalReview,
    draft_digest,
    load_latest_us_proposal,
    load_us_proposal,
    prepare_us_proposal,
    render_reviewed_report,
    reviewed_report,
    save_us_proposal,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _unique_fields(fields: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("duplicate JSON field")
    return result


def _read(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields, parse_float=Decimal
    )


def _save(path: Path, draft: ReportDraft, review: ReportReview) -> None:
    saved = reviewed_report(draft, review)
    content = canonical_json(saved.model_dump(mode="json")).encode() + b"\n"
    # One create-only private file. Do not replace a draft, prior report or symlink.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "FAIL arguments\n")


def build_parser() -> _Parser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("draft-digest").add_argument("draft", type=Path)
    save = commands.add_parser("save")
    save.add_argument("draft", type=Path)
    save.add_argument("review", type=Path)
    save.add_argument("--output", type=Path, required=True)
    commands.add_parser("render").add_argument("saved", type=Path)
    check = commands.add_parser("check-render")
    check.add_argument("saved", type=Path)
    check.add_argument("rendered", type=Path)
    digest = commands.add_parser("proposal-digest", help="bind a USD decision to reviewed analysis")
    publish = commands.add_parser("proposal-save", help="save the reviewed decision in an App copy")
    for command in (digest, publish):
        command.add_argument("saved_analysis", type=Path)
        command.add_argument("decision", type=Path)
    publish.add_argument("proposal_review", type=Path)
    publish.add_argument("--db", type=Path, required=True)
    show = commands.add_parser("proposal-show", help="read an exact or latest saved US decision")
    show.add_argument("--db", type=Path, required=True)
    show.add_argument("--proposal-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "draft"
    try:
        if args.command in {"draft-digest", "save"}:
            draft = ReportDraft.model_validate(_read(args.draft))
            if args.command == "save":
                stage = "review/save"
                _save(args.output, draft, ReportReview.model_validate(_read(args.review)))
            else:
                print(
                    canonical_json(
                        {"report_id": draft.report_id, "draft_sha256": draft_digest(draft)}
                    )
                )
        elif args.command in {"render", "check-render"}:
            stage = "saved report"
            saved = ReviewedReport.model_validate(_read(args.saved))
            content = render_reviewed_report(saved)
            if args.command == "render":
                sys.stdout.buffer.write(content)
            else:
                stage = "rendered file"
                if args.rendered.read_bytes() != content:
                    raise ValueError("rendered content differs")
                print("PASS exact private analytical report render")
        elif args.command == "proposal-show":
            stage = "US App read"
            with closing(connect_read_only(args.db)) as connection:
                record = (
                    load_latest_us_proposal(connection)
                    if args.proposal_id is None
                    else load_us_proposal(connection, args.proposal_id)
                )
            print(canonical_json(None if record is None else record.model_dump(mode="json")))
            return 1 if record is None else 0
        else:
            stage = "US proposal draft"
            analysis = ReviewedReport.model_validate(_read(args.saved_analysis))
            decision = USProposalDraft.model_validate(_read(args.decision))
            if args.command == "proposal-digest":
                digest_value = prepare_us_proposal(decision, analysis)
            else:
                stage = "US proposal save"
                review = USProposalReview.model_validate(_read(args.proposal_review))
                saved_proposal = save_us_proposal(
                    args.db, decision, analysis, review, published_at=datetime.now(UTC)
                )
                digest_value = saved_proposal.review.draft_sha256
            print(
                canonical_json({"proposal_id": decision.proposal_id, "draft_sha256": digest_value})
            )
    except (OSError, ValueError, TypeError, ArithmeticError, RuntimeError, sqlite3.Error):
        print(f"FAIL {stage}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
