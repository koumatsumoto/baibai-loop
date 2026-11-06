"""保存済み旧runの読取と明示cache整理。再計算の入口は持たない。"""

from __future__ import annotations

import argparse
from pathlib import Path

from baibai_engine.cli.environment import load_project_env
from baibai_engine.cli.historical_evaluation.prune import prune_command
from baibai_engine.cli.historical_evaluation.query import candidates_show_command, run_show_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine historical-evaluation")
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

    raise AssertionError(f"unreachable command: {args.command!r}")
