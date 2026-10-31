"""提案訂正と人間の報告の明示保存。発注・採用・ledger更新は行わない。"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    HumanProposalReportDraft,
    ProposalCorrection,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.primitives.json import canonical_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine investment-judgment proposal-records")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    correct = commands.add_parser("correct", help="exclude an erroneous exact proposal from latest")
    correct.add_argument("draft", type=Path)
    correct.add_argument("--expected-latest-id", required=True)
    correct.add_argument("--expected-latest-sha256", required=True)
    report = commands.add_parser("report", help="save a human not-purchased report without trades")
    report.add_argument("draft", type=Path)
    show = commands.add_parser("show", help="read correction and human report history")
    show.add_argument("proposal_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        service = ProposalRecordService(args.db)
        payload: Any
        if args.command == "show":
            payload = service.read(args.proposal_id)
        else:
            raw = yaml.safe_load(args.draft.read_text(encoding="utf-8"))
            record: ProposalCorrection | HumanProposalReport
            if args.command == "correct":
                record = service.correct(
                    ProposalCorrectionDraft.model_validate_json(canonical_json(raw)),
                    expected_latest_ref=ProposalReference(
                        proposal_id=args.expected_latest_id,
                        draft_sha256=args.expected_latest_sha256,
                    ),
                )
            else:
                record = service.report(
                    HumanProposalReportDraft.model_validate_json(canonical_json(raw))
                )
            payload = record.model_dump(mode="json")
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
