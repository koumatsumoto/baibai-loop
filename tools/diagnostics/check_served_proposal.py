"""照合済みのAppコピーと取得済みの提案表示を読む。取得・配備や新しい提案は行わない。"""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Never

from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.query import (
    ProposalRecordReader,
    load_investment_proposal,
)
from baibai_engine.investment_judgment.workflow.operation.progress import (
    fixed_proposal_scope,
    progress_binding,
    selection_binding,
)
from baibai_engine.investment_judgment.workflow.operation.research_binding import (
    load_scope_operation,
)
from baibai_engine.investment_judgment.workflow.selection_evidence import (
    read_selection_evidence_from_connection,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.proposal_models import SavedProposalView
from baibai_web.sources.proposal import read_saved_proposal_view


def _fixed_case_completed(connection: sqlite3.Connection, proposal: TradeProposal) -> bool:
    published = proposal.research_scope
    case = load_scope_operation(connection, published.operation_id)
    selected = selection_binding(case.payload)
    progress = progress_binding(case.payload)
    if (
        selected is None
        or selected.request.scope != proposal.request_scope
        or case.started_at > proposal.published_at
        or (case.completed_at is not None and case.completed_at < proposal.published_at)
    ):
        raise ValueError
    actual = fixed_proposal_scope(case.operation_id, selected, progress)
    if actual != published:
        # 移行済みCaseの原公開hashだけは、既存の照合が通る場合に限り異なってよい。
        if actual.model_copy(update={"selection_sha256": published.selection_sha256}) != published:
            raise ValueError
        evidence = read_selection_evidence_from_connection(
            connection,
            operation_id=case.operation_id,
            sha256=published.selection_sha256,
            proposal_id=proposal.proposal_id,
        )
        if (
            evidence["role"] != "as_published"
            or evidence["operational_selection_sha256"] != actual.selection_sha256
        ):
            raise ValueError
    ref = progress.proposal_ref
    return (
        case.status == "completed"
        and ref is not None
        and ref.proposal_id == proposal.proposal_id
        and ref.draft_sha256 == proposal.review.draft_sha256
    )


class _Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "FAIL arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Arguments(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--latest-json", required=True, type=Path)
    parser.add_argument("--history-json", required=True, type=Path)
    parser.add_argument(
        "--proposal-id", help="require this exact ID to be the App's official latest"
    )
    parser.add_argument(
        "--require-completed",
        action="store_true",
        help="also require this publication to be the Case completion reference",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "app_publication"
    try:
        with closing(connect_read_only(args.db)) as connection:
            connection.execute("BEGIN")
            records = ProposalRecordReader(connection)
            fixed = records.latest_id()
            if fixed is None or (args.proposal_id is not None and fixed != args.proposal_id):
                raise ValueError
            proposal = load_investment_proposal(connection, fixed)
            expected = read_saved_proposal_view(connection, fixed, records)
            projection = encode_json(expected.model_dump(mode="json"))
            print(f"PASS {stage}")
            stage = "captured_latest"
            latest = json.loads(args.latest_json.read_bytes())
            if not (
                latest["status"] == "ready"
                and latest["error"] is None
                and expected.saved_market_context is not None
                and expected.saved_market_context.selection_sha256
                == proposal.research_scope.selection_sha256
                and encode_json(latest["proposal"]) == projection
            ):
                raise ValueError
            print(f"PASS {stage}")
            stage = "captured_history"
            history = json.loads(args.history_json.read_bytes())
            rows = history["proposals"]
            if not (
                history["status"] == "ready"
                and history["error"] is None
                and history["unavailable_proposal_ids"] == []
                and isinstance(rows, list)
                and any(
                    row["proposal_id"] == fixed and encode_json(row) == projection for row in rows
                )
            ):
                raise ValueError
            # 履歴は発行時刻の順。IDの集合だけでは順序や各レポートの発行時刻を照合できない。
            # Appの本文全部を再投影せず、発行索引だけを同じ読取transactionで確認する。
            app_index = [
                (datetime.fromisoformat(stamp), key)
                for key, stamp in connection.execute(
                    "SELECT proposal_id,published_at FROM investment_proposal"
                )
            ]
            if any(published.utcoffset() is None for published, _ in app_index):
                raise ValueError
            app_index.sort(reverse=True)
            captured_index: list[tuple[datetime, str]] = []
            for row in rows:
                if row["proposal_id"] == fixed:
                    saved = expected
                else:
                    saved = SavedProposalView.model_validate_json(encode_json(row))
                    # Model validation alone can coerce a timestamp or fill omitted collections.
                    wire_clock = saved.model_dump(mode="json", include={"published_at"})
                    if not (
                        row["published_at"] == wire_clock["published_at"]
                        and saved.items
                        and {"corrections", "human_reports"} <= saved.model_fields_set
                    ):
                        raise ValueError
                captured_index.append((saved.published_at, saved.proposal_id))
            if captured_index != app_index:
                raise ValueError
            print(f"PASS {stage}")
            stage = "matching_view_timestamps"
            stamp = datetime.fromisoformat(latest["generated_at"])
            if (
                stamp.utcoffset() is None
                or history["generated_at"] != latest["generated_at"]
                or stamp < app_index[0][0]
            ):
                raise ValueError
            print(f"PASS {stage}")
            stage = "fixed_case"
            completed = _fixed_case_completed(connection, proposal)
            print(f"PASS {stage}")
            if args.require_completed:
                stage = "completed_case"
                if not completed:
                    raise ValueError
                print(f"PASS {stage}")
    except Exception:
        # Validation errors can contain private report data, hashes and paths.
        print(f"FAIL {stage}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
