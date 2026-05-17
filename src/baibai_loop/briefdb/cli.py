from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

from .coverage import check_coverage
from .db import DEFAULT_DB_PATH, initialize_database
from .generate import BriefDBCoverageError, generate_weekly
from .migrate import migrate_briefs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-briefdb")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize the brief quantitative DB")
    init_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    migrate_parser = subparsers.add_parser(
        "migrate-briefs",
        help="load numeric observations from existing brief YAML files",
    )
    migrate_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    migrate_parser.add_argument("--root", type=Path, default=Path.cwd())
    migrate_parser.add_argument("--brief-root", type=Path, default=Path("records/02-brief"))

    coverage_parser = subparsers.add_parser(
        "check-coverage",
        help="fail if required quantitative observations are missing",
    )
    coverage_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    coverage_parser.add_argument("--kind", required=True)
    coverage_parser.add_argument("--start", required=True, type=date.fromisoformat)
    coverage_parser.add_argument("--end", required=True, type=date.fromisoformat)

    generate_parser = subparsers.add_parser(
        "generate-weekly",
        help="print a world-weekly quantitative YAML fragment from the DB",
    )
    generate_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    generate_parser.add_argument("--start", required=True, type=date.fromisoformat)
    generate_parser.add_argument("--end", required=True, type=date.fromisoformat)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    match args.command:
        case "init":
            conn = initialize_database(args.db)
            conn.close()
            print(f"initialized {args.db}")
            return 0
        case "migrate-briefs":
            migration = migrate_briefs(args.root, args.db, brief_root=args.brief_root)
            print(
                f"brief_files={migration.brief_files} sources={migration.sources} "
                f"indicators={migration.indicators} observations={migration.observations}"
            )
            return 0
        case "check-coverage":
            coverage = check_coverage(args.db, kind=args.kind, start=args.start, end=args.end)
            if coverage.ok:
                print(f"coverage ok: {args.kind} {args.start}..{args.end}")
                return 0
            for finding in coverage.findings:
                print(f"error: {finding.message}", file=sys.stderr)
            return 1
        case "generate-weekly":
            try:
                payload = generate_weekly(args.db, start=args.start, end=args.end)
            except BriefDBCoverageError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), end="")
            return 0
    raise AssertionError(f"unreachable command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
