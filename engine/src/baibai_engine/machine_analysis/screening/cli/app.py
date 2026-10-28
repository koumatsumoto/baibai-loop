"""保存済み旧run・較正のCLI。新しい候補政策とsource取得は別入口。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import cast

from baibai_engine.primitives.dates import parse_iso_date as _parse_iso_date
from baibai_engine.cli.environment import load_project_env
from baibai_engine.machine_analysis.historical_evaluation.cli import (
    calibration_build_command,
    calibration_evaluate_command,
)
from baibai_engine.machine_analysis.historical_evaluation.evidence import DECISION_SUBJECTS
from baibai_engine.machine_analysis.historical_evaluation.panel import (
    PANEL_BUILD_POLICIES,
    PanelVariant,
)
from baibai_engine.machine_analysis.historical_evaluation.store import DEFAULT_CALIBRATION_DIR
from baibai_engine.machine_analysis.screening.rule_config import (
    DEFAULT_RULES_PATH,
    load_screening_rules,
)
from baibai_engine.storage.paths import DEFAULT_SQLITE_CACHE_DIR

from .cache import verify_cache_coverage_command
from .prune import prune_command
from .query import candidates_show_command, run_show_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine screening")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prune_parser = subparsers.add_parser(
        "prune",
        help="delete old screening run cache generations and vacuum the store",
    )
    prune_parser.add_argument(
        "--keep",
        type=int,
        default=3,
        help="number of newest run generations to keep (default: 3)",
    )
    prune_parser.add_argument("--runs-db", help="screening run store path")

    coverage_parser = subparsers.add_parser(
        "verify-cache-coverage",
        help="check that SQLite can serve the legacy calibration input contract",
    )
    coverage_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    coverage_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    coverage_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path for required JPX sources "
        f"(default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    coverage_parser.add_argument(
        "--allow-stale-jpx",
        action="store_true",
        help="degraded verification only; allow old JPX fetched_at_utc snapshots",
    )

    run_show = subparsers.add_parser("run-show", help="export one saved Stock Analysis run")
    candidates = subparsers.add_parser("candidates", help="read candidates from a saved run")
    candidates_commands = candidates.add_subparsers(dest="candidates_command", required=True)
    candidates_show = candidates_commands.add_parser("show", help="show one run's candidates")
    for show in (run_show, candidates_show):
        show.add_argument("--run-revision-id", required=True)
        show.add_argument("--runs-db", help="screening run store path")
        show.add_argument("--output-path", help="also write the same output to this path")
        show.add_argument("--force", action="store_true", help="overwrite an existing output file")
        show.add_argument("--format", choices=("yaml", "json"), default="yaml")

    calibration_build_parser = subparsers.add_parser(
        "calibration-build",
        help=(
            "build retrospective monthly panels and forward returns for "
            "estimate calibration (local cache only)"
        ),
    )
    calibration_build_parser.add_argument(
        "--start", required=True, help="grid start date (YYYY-MM-DD)"
    )

    calibration_build_parser.add_argument("--end", required=True, help="grid end date (YYYY-MM-DD)")
    calibration_build_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    calibration_build_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    calibration_build_parser.add_argument(
        "--calibration-dir",
        default=str(DEFAULT_CALIBRATION_DIR),
        help=f"panel / forward store directory (default: {DEFAULT_CALIBRATION_DIR})",
    )
    calibration_build_parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild panels that already exist in the calibration store",
    )
    calibration_build_parser.add_argument(
        "--panel-variant",
        choices=tuple(PANEL_BUILD_POLICIES),
        default="production",
        help="panel input contract (non-production variants are diagnostic-only)",
    )
    calibration_build_parser.add_argument(
        "--without-control-event-exits",
        action="store_true",
        help=(
            "resolve forward windows from market closes alone, leaving delisted names to "
            "sensitivity scenarios; regenerates the comparison baseline"
        ),
    )
    calibration_build_parser.add_argument(
        "--without-failure-exits",
        action="store_true",
        help=(
            "leave windows ended by a failure delisting unresolved instead of pricing them "
            "at the last close; regenerates the comparison baseline"
        ),
    )

    calibration_evaluate_parser = subparsers.add_parser(
        "calibration-evaluate",
        help="evaluate stored calibration cohorts (rank IC / decile / candidate replay)",
    )
    calibration_evaluate_parser.add_argument(
        "--calibration-dir",
        default=str(DEFAULT_CALIBRATION_DIR),
        help=f"panel / forward store directory (default: {DEFAULT_CALIBRATION_DIR})",
    )
    calibration_evaluate_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    calibration_evaluate_parser.add_argument(
        "--horizon",
        action="append",
        dest="horizons",
        help="horizon to evaluate (3m/6m/1y/3y/5y; repeatable; default: all)",
    )
    calibration_evaluate_parser.add_argument(
        "--run-purpose",
        choices=("diagnostic", "empirical_change_evidence"),
        default="diagnostic",
    )
    calibration_evaluate_parser.add_argument(
        "--required-asof", action="append", dest="required_asofs"
    )
    calibration_evaluate_parser.add_argument(
        "--required-metric", action="append", dest="required_metrics"
    )
    calibration_evaluate_parser.add_argument(
        "--decision-subject",
        choices=DECISION_SUBJECTS,
        default="estimator_policy",
        help="evidence subject whose mandatory metrics cannot be omitted",
    )
    calibration_evaluate_parser.add_argument(
        "--candidate-discovery-approach",
        help="one valuation approach when decision subject is candidate_discovery_approach",
    )
    calibration_evaluate_parser.add_argument(
        "--out",
        help="write the evaluation YAML to this path instead of stdout",
    )
    calibration_evaluate_parser.add_argument(
        "--context-out",
        help="write the expiring E[r] realized-distribution context consumed by review paths",
    )
    calibration_evaluate_parser.add_argument(
        "--start",
        help="evaluate only cohorts on/after this date (YYYY-MM-DD; design/confirm 分割用)",
    )
    calibration_evaluate_parser.add_argument(
        "--end",
        help="evaluate only cohorts on/before this date (YYYY-MM-DD)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    args = parser.parse_args(argv)

    load_project_env()

    if args.command == "prune":
        return prune_command(
            keep=args.keep,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
        )

    if args.command in {"run-show", "candidates"}:
        command = candidates_show_command if args.command == "candidates" else run_show_command
        return command(
            run_revision_id=args.run_revision_id,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
            output_format=args.format,
        )

    if args.command == "calibration-build":
        # calibration-build reads the SQLite store and writes the local
        # calibration store only; no provider credentials are needed.
        return calibration_build_command(
            sqlite_path=Path(args.sqlite_path),
            calibration_dir=Path(args.calibration_dir),
            rules=load_screening_rules(Path(args.rules_path)),
            start=_parse_iso_date(args.start),
            end=_parse_iso_date(args.end),
            force=args.force,
            panel_variant=cast(PanelVariant, args.panel_variant),
            use_control_event_exits=not args.without_control_event_exits,
            use_failure_exits=not args.without_failure_exits,
        )

    if args.command == "calibration-evaluate":
        return calibration_evaluate_command(
            calibration_dir=Path(args.calibration_dir),
            rules=load_screening_rules(Path(args.rules_path)),
            horizons=args.horizons,
            run_purpose=args.run_purpose,
            required_asofs=args.required_asofs,
            required_metrics=args.required_metrics,
            decision_subject=args.decision_subject,
            candidate_discovery_approach=args.candidate_discovery_approach,
            output_path=Path(args.out) if args.out else None,
            context_output_path=Path(args.context_out) if args.context_out else None,
            start=_parse_iso_date(args.start) if args.start else None,
            end=_parse_iso_date(args.end) if args.end else None,
        )

    if args.command == "verify-cache-coverage":
        # verify-cache-coverage is local-only and never reads raw JSON or calls providers.
        rules = load_screening_rules(Path(args.rules_path))
        return verify_cache_coverage_command(
            sqlite_path=Path(args.sqlite_path),
            asof_date=_parse_iso_date(args.asof),
            required_jpx_sources=rules.universe.required_jpx_flags,
            allow_stale_jpx=args.allow_stale_jpx,
        )

    raise AssertionError(f"unreachable command: {args.command!r}")
