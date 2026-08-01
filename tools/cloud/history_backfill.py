"""Run resumable market backfills and publish committed partial progress."""

from __future__ import annotations

import argparse
import hashlib
import subprocess  # nosec B404
from collections.abc import Callable, Sequence
from pathlib import Path

from tools.cloud.sqlite_snapshot import validate_database

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKET_STORE = ROOT / "data/screening/market.sqlite"
CommandRunner = Callable[[Sequence[str]], int]


def _run_command(command: Sequence[str]) -> int:
    # Commands are assembled as argv from fixed executables/subcommands; shell parsing is disabled.
    return subprocess.run(command, check=False).returncode  # nosec B603


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_history_backfill(
    *,
    start: str,
    end: str,
    master_month_end_from: str | None,
    sqlite_path: Path = DEFAULT_MARKET_STORE,
    runner: CommandRunner = _run_command,
) -> int:
    """Publish changed partial progress, then preserve the original backfill failure."""
    before_sha256 = _sha256(sqlite_path)
    history_code = runner(
        (
            "uv",
            "run",
            "baibai-engine",
            "screening",
            "backfill-history",
            "--start",
            start,
            "--end",
            end,
        )
    )
    master_code = 0
    if master_month_end_from is not None:
        master_code = runner(
            (
                "uv",
                "run",
                "baibai-engine",
                "screening",
                "backfill-master",
                "--month-end-from",
                master_month_end_from,
                "--month-end-to",
                end,
            )
        )

    after_sha256 = _sha256(sqlite_path)
    upload_code = 0
    if after_sha256 != before_sha256:
        validate_database(sqlite_path)
        upload_code = runner((str(ROOT / "tools/cloud/r2_transfer.sh"), "push-market"))
    else:
        print("history backfill made no market-store change; upload skipped")

    return history_code or master_code or upload_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--master-month-end-from")
    parser.add_argument("--sqlite-path", type=Path, default=DEFAULT_MARKET_STORE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_history_backfill(
        start=args.start,
        end=args.end,
        master_month_end_from=args.master_month_end_from,
        sqlite_path=args.sqlite_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
