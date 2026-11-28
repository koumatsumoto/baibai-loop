"""保有・運用成績・保存提案を、既存storeから読取専用で書き出す。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from baibai_engine.machine_analysis.observations.stocks.current_reader import (
    CurrentListingSection,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    read_local_listing_master,
)
from baibai_engine.storage.filesystem import write_text_atomic
from baibai_engine.storage.paths import (
    APPLICATION_DB_PATH,
    MARKET_DB_PATH,
    reject_noncanonical_store_paths,
)
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.outcomes import build_outcomes
from baibai_web.sources.db_sources import CurrentJpMarketSource, DbLedgerSource
from baibai_web.sources.proposal import read_serving_proposal_views

_LOGGER = logging.getLogger(__name__)


def export_read_models(
    root: Path, output_dir: Path, *, current_jp_listing: CurrentListingSection | None = None
) -> list[Path]:
    app = root / APPLICATION_DB_PATH
    market_path = root / MARKET_DB_PATH
    ledger = DbLedgerSource(app, market_path)
    market = CurrentJpMarketSource(market_path, listing=current_jp_listing)
    # Build every view before replacing artifacts. Optional display sources may be
    # unavailable; builders retain the independent facts and expose missing sections.
    dashboard = build_dashboard(ledger, market)
    outcomes = build_outcomes(app)
    (latest, history), latest_us = read_serving_proposal_views(app)
    views = {
        "portfolio-dashboard": dashboard,
        "outcomes": outcomes,
        "latest-proposal": latest,
        "proposal-history": history,
        "latest-us-proposal": latest_us,
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
    parser.add_argument(
        "--current-listing-master",
        type=Path,
        help="同じ実行で固定された現行銘柄マスター。未指定なら銘柄コードのみ表示。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reject_noncanonical_store_paths(args.root)
    listing: CurrentListingSection | None = None
    if args.current_listing_master is not None:
        try:
            listing = read_local_listing_master(args.current_listing_master).get("JP")
        except (OSError, ValueError) as error:
            _LOGGER.warning(
                "現行の銘柄マスターを読み取れないためtickerで表示します (%s)",
                type(error).__name__,
            )
    for path in export_read_models(args.root, args.output_dir, current_jp_listing=listing):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
