"""完了した一回の判断を、将来比較の開始材料としてread-onlyで取り出す。

保存済みの母集団・候補・選定・提案を使い、最新モデルや現在価格で埋めない。
未選定・調査未完了・経済的見送りを区別する。採用・約定・成果は測定しない。
出力は再生成できる診断であり、第二の判断正本や正式な較正artifactではない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.query import (
    has_published_proposal_for_operation,
    load_investment_proposal,
)
from baibai_engine.investment_judgment.workflow.operation.progress import (
    progress_binding,
    proposal_scope,
    require_coverage,
    selection_binding,
)
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    GeneratedResearchSelectionV4,
    selection_sha256,
    selection_targets,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.filesystem import write_text_new
from baibai_engine.storage.sqlite.application.read import connect_read_only


def inspect_decision_cohort(*, db_path: Path, operation_id: str) -> dict[str, object]:
    """One immutable completed Operation; no current stores, network or writes."""
    operation = OperationService(db_path).get(operation_id)
    if operation.status != "completed":
        raise ValueError("decision cohort requires a completed immutable Operation")
    assert operation.completed_at is not None
    selected = selection_binding(operation.payload)
    if not isinstance(selected, (GeneratedResearchSelection, GeneratedResearchSelectionV4)):
        raise ValueError("decision cohort requires saved generated-model Selection inputs")
    screening = selected.inputs.screening_selection
    if screening is not None and not isinstance(screening.result, EconomicPathSelection):
        raise ValueError("legacy screening results are not current economic-path evidence")
    progress = progress_binding(operation.payload)
    require_coverage(selection_targets(selected), progress.outcomes, complete=True)
    scope = proposal_scope(operation_id, operation.payload) if selected.targets else None
    proposal = None
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        if progress.proposal_ref is not None:
            proposal = load_investment_proposal(connection, progress.proposal_ref.proposal_id)
            if (
                proposal_draft_sha256(proposal) != progress.proposal_ref.draft_sha256
                or proposal.research_scope != scope
                or proposal.request_scope != selected.request.scope
            ):
                raise ValueError("saved Proposal differs from the completed Operation reference")
            if not operation.started_at <= proposal.published_at <= operation.completed_at:
                raise ValueError("saved Proposal is outside the completed Operation period")
        elif any(item.status == "reviewed" for item in progress.outcomes) or (
            has_published_proposal_for_operation(connection, operation_id)
        ):
            raise ValueError("completed Operation is missing its published Proposal reference")

    generation = selected.inputs.generation
    result = None if screening is None else screening.result
    assert result is None or isinstance(result, EconomicPathSelection)
    cards = {} if result is None else {card.ticker: card for card in result.cards}
    targets = {target.ticker: target for target in selected.targets}
    outcomes = {outcome.ticker: outcome for outcome in progress.outcomes}
    items = {} if proposal is None else {item.ticker: item for item in proposal.items}
    tickers = (*cards, *(ticker for ticker in targets if ticker not in cards))
    rows = []
    for ticker in tickers:
        card, target, outcome, item = (
            cards.get(ticker),
            targets.get(ticker),
            outcomes.get(ticker),
            items.get(ticker),
        )
        origin = None
        if item is not None:
            assert proposal is not None
            assert progress.proposal_ref is not None
            origin = {
                "proposal_id": proposal.proposal_id,
                "draft_sha256": progress.proposal_ref.draft_sha256,
                "ticker": ticker,
            }
        rows.append(
            {
                "ticker": ticker,
                "in_screening_universe": None
                if screening is None
                else ticker in screening.universe_tickers,
                "candidate": None if card is None else asdict(card),
                "research_target": None if target is None else target.model_dump(mode="json"),
                "research_status": "not_selected" if outcome is None else outcome.status,
                "research_outcome": None if outcome is None else outcome.model_dump(mode="json"),
                "proposal_item": None if item is None else item.model_dump(mode="json"),
                "retrospective_origin": origin,
            }
        )
    baseline: dict[str, object] = {
        "kind": "completed_decision_cohort_diagnostic",
        "operation_id": operation_id,
        "operation_sha256": sha256(canonical_json(operation.public()).encode()).hexdigest(),
        "selection_sha256": selection_sha256(selected),
        "selection_schema_version": selected.schema_version,
        "request_scope": selected.request.scope,
        "research_as_of": selected.research_as_of.isoformat(),
        "input_known_at": selected.input_known_at.isoformat(),
        "operation_started_at": operation.started_at.isoformat(),
        "baseline_available_at": operation.completed_at.isoformat(),
        "selection_summary": selected.selection_summary,
        "operation_result": operation.payload.result,
        "generation": {
            "generation_id": generation.generation_id,
            "as_of": generation.as_of.isoformat(),
            "evaluation_time": generation.evaluation_time.isoformat(),
            "inputs": {
                name: value.model_dump(mode="json") for name, value in generation.inputs.items()
            },
            "retained_model_revisions": [
                {
                    "model_id": model.model_id,
                    "subject": model.subject,
                    "calculation_revision": model.calculation_revision,
                }
                for model in generation.models
            ],
        },
        "screening_selection_id": selected.inputs.screening_selection_id,
        "screening_published_at": None
        if selected.inputs.screening_published_at is None
        else selected.inputs.screening_published_at.isoformat(),
        "screening_policy": None if result is None else result.policy.model_dump(mode="json"),
        "screening_universe_tickers": None if screening is None else screening.universe_tickers,
        "eligible_count": None if result is None else result.eligible_count,
        "proposal_ref": None
        if progress.proposal_ref is None
        else progress.proposal_ref.model_dump(mode="json"),
        "proposal_published_at": None if proposal is None else proposal.published_at.isoformat(),
        "row_scope": "screening_candidates_union_selected_targets",
        "rows": rows,
        "not_measured": [
            "historical_point_in_time_guarantee",
            "prospective_enrollment_before_outcomes",
            "other_operations_including_unfinished_or_abandoned",
            "screening_universe_row_details",
            "unselected_candidate_rejection_reason",
            "later_corrections_or_retrospectives",
            "human_adoption_or_execution",
            "forward_returns_or_policy_effectiveness",
        ],
    }
    return {
        "baseline": baseline,
        "baseline_sha256": sha256(canonical_json(baseline).encode()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="private application DB copy")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--out", type=Path, help="new private JSON file; never overwrite")
    args = parser.parse_args(argv)
    try:
        if args.out is not None and (args.out.exists() or args.out.is_symlink()):
            raise FileExistsError(f"output already exists: {args.out}")
        payload = inspect_decision_cohort(db_path=args.db, operation_id=args.operation_id)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.out is None:
            sys.stdout.write(content)
        else:
            write_text_new(args.out, content)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        # Validation errors may embed private judgment values; never log their body.
        print(f"error: decision cohort inspection failed ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
