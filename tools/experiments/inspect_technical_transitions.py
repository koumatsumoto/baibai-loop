"""二つの保存GenerationでTAの状態と変化を分ける読取専用研究診断。

現行入口の閾値は変えず、経路ごとの非合致→合致を比較する。日次政策、
財務gate、Research Selectionや売買へ適用せず、当時PITや利益も証明しない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import cast

from pydantic import JsonValue
from tools.experiments.technical_discovery import inspect_technical_discovery

from baibai_engine.storage.filesystem import write_text_new

COMPARISON_REVISION = "technical-state-transitions-research-v1"
_HEADER_FIELDS = ("generation_id", "as_of", "evaluation_time", "policy_id")
_KNOWN_TRANSITIONS = {
    ("not_matched", "matched"): "new_match",
    ("matched", "matched"): "continuing_match",
    ("matched", "not_matched"): "ended_match",
    ("not_matched", "not_matched"): "continuing_nonmatch",
}
_TRANSITIONS = (
    *_KNOWN_TRANSITIONS.values(),
    "input_became_observable",
    "input_became_unobservable",
    "input_unresolved",
    "not_comparable",
)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _rows(snapshot: dict[str, JsonValue], generation_id: str) -> dict[str, dict[str, JsonValue]]:
    if snapshot["generation_id"] != generation_id:
        raise ValueError("requested generation differs from technical discovery")
    rows = cast(list[dict[str, JsonValue]], snapshot["rows"])
    by_ticker = {cast(str, row["ticker"]): row for row in rows}
    if len(by_ticker) != len(rows) or any(
        row[field] != snapshot[field] for row in rows for field in _HEADER_FIELDS
    ):
        raise ValueError("technical discovery rows do not share one generation and policy")
    return by_ticker


def _global_reasons(previous: dict[str, JsonValue], current: dict[str, JsonValue]) -> list[str]:
    reasons = []
    if previous["generation_id"] == current["generation_id"]:
        reasons.append("same_generation")
    if previous["policy_id"] != current["policy_id"]:
        reasons.append("discovery_policy_mismatch")
    before = datetime.fromisoformat(cast(str, previous["evaluation_time"]))
    after = datetime.fromisoformat(cast(str, current["evaluation_time"]))
    if before.utcoffset() is None or after.utcoffset() is None:
        raise ValueError("generation evaluation time requires a timezone")
    if after <= before:
        reasons.append("generation_evaluation_not_increasing")
    if date.fromisoformat(cast(str, current["as_of"])) < date.fromisoformat(
        cast(str, previous["as_of"])
    ):
        reasons.append("as_of_moved_backwards")
    return reasons


def _comparison_reasons(
    previous: dict[str, JsonValue] | None,
    current: dict[str, JsonValue] | None,
    global_reasons: list[str],
) -> list[str]:
    reasons = list(global_reasons)
    if previous is None or current is None:
        return [
            *reasons,
            "absent_from_previous_plan" if previous is None else "absent_from_current_plan",
        ]
    if previous["missing_models"] or current["missing_models"]:
        reasons.append("model_not_generated")
    if previous["model_revisions"] != current["model_revisions"]:
        reasons.append("model_revision_mismatch")
    for label, row in (("previous", previous), ("current", current)):
        if not _object(row["price_context"]).get("ready"):
            reasons.append(f"{label}_price_not_ready")
    previous_day, current_day = previous["expected_price_date"], current["expected_price_date"]
    if not isinstance(previous_day, str) or not isinstance(current_day, str):
        reasons.append("expected_price_date_unavailable")
    elif current_day == previous_day:
        reasons.append("same_price_date")
    elif current_day < previous_day:
        reasons.append("price_date_moved_backwards")
    else:
        # R1's calendar endpoints establish adjacency, not its return or G60's peak date.
        window = _object(_object(_object(current["observations"]).get("R1")).get("window"))
        if (
            window.get("window_start_date") != previous_day
            or window.get("window_end_date") != current_day
        ):
            reasons.append("adjacent_price_sessions_not_established")
    return reasons


def _route_transition(
    previous: dict[str, JsonValue], current: dict[str, JsonValue], *, comparable: bool
) -> str:
    if not comparable:
        return "not_comparable"
    states = (previous.get("state"), current.get("state"))
    before_known, after_known = (
        route.get("state") in ("matched", "not_matched") and not route.get("missing_conditions")
        for route in (previous, current)
    )
    if before_known and after_known:
        return _KNOWN_TRANSITIONS[cast(tuple[str, str], states)]
    if after_known:
        return "input_became_observable"
    return "input_became_unobservable" if before_known else "input_unresolved"


def inspect_technical_transitions(
    *, previous_generation_id: str, current_generation_id: str, runs_path: Path
) -> dict[str, JsonValue]:
    """Retain both saved plans and source diagnostics; never resolve latest or write stores."""
    previous = inspect_technical_discovery(
        generation_id=previous_generation_id, runs_path=runs_path
    )
    current = inspect_technical_discovery(generation_id=current_generation_id, runs_path=runs_path)
    before = _rows(previous, previous_generation_id)
    after = _rows(current, current_generation_id)
    global_reasons = _global_reasons(previous, current)
    rows: list[JsonValue] = []
    membership_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    route_counts: dict[str, Counter[str]] = {}
    comparable_count = current_matches = paired_current_matches = new_matches = existing_new = 0
    for ticker in sorted(before.keys() | after.keys()):
        left, right = before.get(ticker), after.get(ticker)
        membership = (
            "both"
            if left is not None and right is not None
            else "current_only"
            if right is not None
            else "previous_only"
        )
        membership_counts[membership] += 1
        reasons = _comparison_reasons(left, right, global_reasons)
        reason_counts.update(reasons)
        comparable = not reasons
        comparable_count += comparable
        left_routes = {} if left is None else _object(left["routes"])
        right_routes = {} if right is None else _object(right["routes"])
        changes: dict[str, JsonValue] = {}
        new_routes: list[JsonValue] = []
        for name in sorted(left_routes.keys() | right_routes.keys()):
            left_route, right_route = (
                _object(left_routes.get(name)),
                _object(right_routes.get(name)),
            )
            transition = _route_transition(left_route, right_route, comparable=comparable)
            counts = route_counts.setdefault(name, Counter())
            counts[transition] += 1
            counts["previous_matched"] += left_route.get("state") == "matched"
            counts["current_matched"] += right_route.get("state") == "matched"
            changes[name] = {
                "transition": transition,
                "previous_state": left_route.get("state"),
                "current_state": right_route.get("state"),
            }
            if transition == "new_match":
                new_routes.append(name)
        current_match = right is not None and bool(right["matched_routes"])
        current_matches += current_match
        paired_current_matches += current_match and comparable
        new_matches += bool(new_routes)
        existing_new += bool(new_routes) and left is not None and bool(left["matched_routes"])
        rows.append(
            {
                "ticker": ticker,
                "plan_membership": membership,
                "comparison": {"comparable": comparable, "reasons": list(reasons)},
                "newly_matched_routes": new_routes,
                "route_changes": changes,
                "previous": left,
                "current": right,
            }
        )
    return {
        "kind": "technical_discovery_transition_research_diagnostic",
        "comparison_revision": COMPARISON_REVISION,
        "adoption_status": "unadopted_research_only",
        "read_semantics": "saved_generations_not_point_in_time_reconstruction",
        "row_scope": "union_of_two_saved_stock_plans_no_ranking_or_quota",
        "generation_comparison_reasons": list(global_reasons),
        "previous": {field: previous[field] for field in _HEADER_FIELDS},
        "current": {field: current[field] for field in _HEADER_FIELDS},
        "summary": {
            "previous_planned_stocks": len(before),
            "current_planned_stocks": len(after),
            "union_planned_stocks": len(rows),
            "plan_membership": {
                state: membership_counts[state]
                for state in ("both", "previous_only", "current_only")
            },
            "comparable_price_pairs": comparable_count,
            "comparison_reason_counts": dict(sorted(reason_counts.items())),
            "current_matched_any_route": current_matches,
            "current_matched_any_route_in_comparable_pairs": paired_current_matches,
            "new_match_any_route": new_matches,
            "new_match_already_in_previous_union": existing_new,
            "routes": {
                name: {
                    "previous_matched": counts["previous_matched"],
                    "current_matched": counts["current_matched"],
                    "known_state_pairs": sum(
                        counts[state] for state in _KNOWN_TRANSITIONS.values()
                    ),
                    "transitions": {state: counts[state] for state in _TRANSITIONS},
                }
                for name, counts in sorted(route_counts.items())
            },
        },
        "rows": rows,
        "not_measured": [
            "original_publication_vintage_or_consumer_receipt_time",
            "prospective_enrollment_or_tradable_event_time",
            "financial_gate_or_l3_research_selection",
            "forward_returns_or_policy_effectiveness",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-generation-id", required=True)
    parser.add_argument("--current-generation-id", required=True)
    parser.add_argument("--runs-path", type=Path, required=True)
    parser.add_argument(
        "--out", type=Path, required=True, help="new private JSON file; never overwrite"
    )
    args = parser.parse_args(argv)
    try:
        if args.out.exists() or args.out.is_symlink():
            raise FileExistsError("output already exists")
        result = inspect_technical_transitions(
            previous_generation_id=args.previous_generation_id,
            current_generation_id=args.current_generation_id,
            runs_path=args.runs_path,
        )
        write_text_new(
            args.out, json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        return 0
    except (EOFError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(
            f"error: technical transition inspection failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
