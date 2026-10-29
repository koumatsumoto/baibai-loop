"""保有・運用成績・タスク・保存提案を、既存storeから読取専用で書き出す。"""

from __future__ import annotations

import argparse
from pathlib import Path

from baibai_engine.storage.filesystem import write_text_atomic
from baibai_engine.storage.paths import (
    APPLICATION_DB_PATH,
    MARKET_DB_PATH,
    reject_noncanonical_store_paths,
)
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.outcomes import build_outcomes
from baibai_web.readmodel.builders.proposal import build_latest_proposal
from baibai_web.readmodel.builders.tasks import build_tasks
from baibai_web.sources.db_sources import DbLedgerSource, DbMarketSource, DbTaskSource
from baibai_web.sources.proposal import DbProposalSource


def export_read_models(root: Path, output_dir: Path) -> list[Path]:
    app = root / APPLICATION_DB_PATH
    market_path = root / MARKET_DB_PATH
    ledger = DbLedgerSource(app, market_path)
    market = DbMarketSource(market_path)
    # Build every view before replacing artifacts. Optional display sources may be
    # unavailable; builders retain the independent facts and expose missing sections.
    views = {
        "portfolio-dashboard": build_dashboard(ledger, market),
        "tasks": build_tasks(DbTaskSource(app), ledger, market),
        "outcomes": build_outcomes(app),
        "latest-proposal": build_latest_proposal(DbProposalSource(app)),
    }
    directory = output_dir / "views"
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, model in views.items():
        path = directory / f"{name}.json"
        write_text_atomic(path, model.model_dump_json(indent=2) + "\n")
        paths.append(path)
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reject_noncanonical_store_paths(args.root)
    for path in export_read_models(args.root, args.output_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
