"""Forward-measurement subcommands for `baibai-loop-screening`.

Replay recorded weekly candidates against the SQLite market cache and score
forward return per evidence lane and selection profile. The commands are
local-only (SQLite cache + recorded candidate YAML), so they dispatch without
provider credentials and `screening/cli/app.py` composes them alongside the
machine-screening commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from baibai_loop.screening.config import DEFAULT_SQLITE_CACHE_DIR
from baibai_loop.screening.forward.cohort_scorecard import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_MIN_RESOLVED,
    render_scorecard_summary,
    run_lane_scorecard,
    run_proposal_scorecard,
    scorecard_to_payload,
)
from baibai_loop.screening.forward.lane_cohorts import (
    DEFAULT_COHORT_HORIZON_WEEKS,
    lane_cohorts_to_payload,
    render_lane_cohort_summary,
    run_lane_cohorts,
)
from baibai_loop.screening.forward.screening_replay import replay_to_payload, run_replay
from baibai_loop.screening.forward.selection_ablation import (
    ablation_to_payload,
    render_ablation_summary,
    run_selection_ablation,
)
from baibai_loop.screening.forward.weeks import discover_week_specs
from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

FORWARD_COMMANDS: frozenset[str] = frozenset(
    {
        "screening-replay",
        "lane-cohorts",
        "lane-scorecard",
        "proposal-scorecard",
        "selection-ablation",
    }
)


def add_subparsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the forward-measurement subcommands on the screening CLI parser."""
    replay_parser = subparsers.add_parser(
        "screening-replay",
        help="replay selection profiles over weekly candidates and score forward return",
    )
    replay_parser.add_argument("--root", type=Path, default=Path.cwd())
    replay_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    replay_parser.add_argument(
        "--profiles",
        default="balanced",
        help="comma-separated selection profiles (default: balanced)",
    )
    replay_parser.add_argument("--top", type=int, default=10, help="candidates per profile")
    replay_parser.add_argument(
        "--holdout-weeks", type=int, default=2, help="trailing weeks to flag as hold-out"
    )
    replay_parser.add_argument(
        "--out", type=Path, help="write replay payload YAML to this path instead of stdout"
    )
    replay_parser.add_argument(
        "--rules-path", type=Path, default=DEFAULT_RULES_PATH, help="screening rules path"
    )
    replay_parser.add_argument(
        "--regime-lens",
        choices=("on", "off"),
        default="on",
        help="apply the market regime lens per replay week (default: on)",
    )
    cohort_parser = subparsers.add_parser(
        "lane-cohorts",
        help="aggregate forward returns per evidence lane over all weekly candidates",
    )
    cohort_parser.add_argument("--root", type=Path, default=Path.cwd())
    cohort_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    cohort_parser.add_argument(
        "--horizons",
        default=",".join(str(weeks) for weeks in DEFAULT_COHORT_HORIZON_WEEKS),
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    cohort_parser.add_argument(
        "--out", type=Path, help="write cohort payload YAML to this path instead of stdout"
    )
    scorecard_parser = subparsers.add_parser(
        "lane-scorecard",
        help="pool candidate forward relatives across weeks into keep/kill/review lane decisions",
    )
    scorecard_parser.add_argument("--root", type=Path, default=Path.cwd())
    scorecard_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    scorecard_parser.add_argument(
        "--horizons",
        default=",".join(str(weeks) for weeks in DEFAULT_COHORT_HORIZON_WEEKS),
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    scorecard_parser.add_argument(
        "--min-resolved",
        type=int,
        default=DEFAULT_MIN_RESOLVED,
        help=f"minimum pooled resolved count before a lane gets a keep/kill decision "
        f"(default: {DEFAULT_MIN_RESOLVED})",
    )
    scorecard_parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
        help=f"bootstrap resamples for the CI (default: {DEFAULT_BOOTSTRAP_ITERATIONS})",
    )
    scorecard_parser.add_argument(
        "--out", type=Path, help="write scorecard payload YAML to this path instead of stdout"
    )
    proposal_parser = subparsers.add_parser(
        "proposal-scorecard",
        help="score the recommended queue (proposal level) vs the all-candidates baseline",
    )
    proposal_parser.add_argument("--root", type=Path, default=Path.cwd())
    proposal_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    proposal_parser.add_argument(
        "--horizons",
        default=",".join(str(weeks) for weeks in DEFAULT_COHORT_HORIZON_WEEKS),
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    proposal_parser.add_argument(
        "--top", type=int, default=10, help="recommended queue size per week (default 10)"
    )
    proposal_parser.add_argument(
        "--profile", default="balanced", help="selection profile (default: balanced)"
    )
    proposal_parser.add_argument(
        "--rules-path", type=Path, default=DEFAULT_RULES_PATH, help="screening rules path"
    )
    proposal_parser.add_argument(
        "--min-resolved",
        type=int,
        default=DEFAULT_MIN_RESOLVED,
        help=f"minimum pooled resolved count before a decision (default: {DEFAULT_MIN_RESOLVED})",
    )
    proposal_parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
        help=f"bootstrap resamples for the CI (default: {DEFAULT_BOOTSTRAP_ITERATIONS})",
    )
    proposal_parser.add_argument(
        "--out", type=Path, help="write scorecard payload YAML to this path instead of stdout"
    )
    ablation_parser = subparsers.add_parser(
        "selection-ablation",
        help="replay selection variants that disable one feature each and score forward return",
    )
    ablation_parser.add_argument("--root", type=Path, default=Path.cwd())
    ablation_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    ablation_parser.add_argument(
        "--top", type=int, default=5, help="recommended queue size per variant (default 5)"
    )
    ablation_parser.add_argument(
        "--profile", help="selection profile (default: rules.selection.default_profile)"
    )
    ablation_parser.add_argument(
        "--horizons",
        default="1,4",
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    ablation_parser.add_argument(
        "--rules-path", type=Path, default=DEFAULT_RULES_PATH, help="screening rules path"
    )
    ablation_parser.add_argument(
        "--out", type=Path, help="write ablation payload YAML to this path instead of stdout"
    )


def run_command(args: argparse.Namespace) -> int:
    """Dispatch a forward-measurement command parsed by the screening CLI."""
    if args.command == "screening-replay":
        return _run_screening_replay(args)
    if args.command == "lane-cohorts":
        return _run_lane_cohorts(args)
    if args.command == "lane-scorecard":
        return _run_lane_scorecard(args)
    if args.command == "proposal-scorecard":
        return _run_proposal_scorecard(args)
    if args.command == "selection-ablation":
        return _run_selection_ablation(args)
    raise AssertionError(f"unreachable forward command: {args.command!r}")


def _run_selection_ablation(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_selection_ablation(
        weeks,
        rules=load_screening_rules(args.rules_path),
        sqlite_path=sqlite_path,
        candidates_root=args.candidates_root,
        ledger_root=args.root / "records",
        profile=args.profile,
        top=args.top,
        horizon_weeks=horizons,
    )
    if args.out is not None:
        text = yaml.safe_dump(ablation_to_payload(result), allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_ablation_summary(result))
    return 0


def _parse_horizons(raw: str) -> tuple[int, ...] | None:
    try:
        horizons = tuple(int(token.strip()) for token in str(raw).split(",") if token.strip())
    except ValueError:
        print("--horizons must be comma-separated integers", file=sys.stderr)
        return None
    if not horizons or any(weeks < 1 for weeks in horizons):
        print("--horizons must include at least one positive week count", file=sys.stderr)
        return None
    return horizons


def _run_lane_cohorts(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_lane_cohorts(
        weeks,
        sqlite_path=sqlite_path,
        horizon_weeks=horizons,
    )
    if args.out is not None:
        payload = lane_cohorts_to_payload(result)
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_lane_cohort_summary(result))
    return 0


def _run_lane_scorecard(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_lane_scorecard(
        weeks,
        sqlite_path=sqlite_path,
        horizon_weeks=horizons,
        min_resolved=args.min_resolved,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    if args.out is not None:
        text = yaml.safe_dump(scorecard_to_payload(result), allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_scorecard_summary(result))
    return 0


def _run_proposal_scorecard(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_proposal_scorecard(
        weeks,
        sqlite_path=sqlite_path,
        rules=load_screening_rules(args.rules_path),
        candidates_root=args.candidates_root,
        ledger_root=args.root / "records",
        horizon_weeks=horizons,
        top=args.top,
        profile=args.profile,
        min_resolved=args.min_resolved,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    if args.out is not None:
        text = yaml.safe_dump(scorecard_to_payload(result), allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_scorecard_summary(result))
    return 0


def _run_screening_replay(args: argparse.Namespace) -> int:
    profiles = [profile.strip() for profile in args.profiles.split(",") if profile.strip()]
    if not profiles:
        print("--profiles must include at least one profile", file=sys.stderr)
        return 1
    weeks = discover_week_specs(args.candidates_root, holdout_weeks=args.holdout_weeks)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_replay(
        weeks,
        profiles=profiles,
        rules=load_screening_rules(args.rules_path),
        sqlite_path=sqlite_path,
        candidates_root=args.candidates_root,
        ledger_root=args.root / "records",
        top=args.top,
        regime_lens=args.regime_lens == "on",
    )
    payload = replay_to_payload(result)
    payload["weeks_meta"] = [
        {"week": spec.asof.isoformat(), "is_holdout": spec.is_holdout} for spec in weeks
    ]
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0
