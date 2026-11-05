"""Internal operational entrypoint for production batch jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from importlib import import_module

from baibai_engine.storage.paths import StoreLayoutError, reject_noncanonical_store_paths

Command = Callable[[list[str] | None], int]

# Resolve after selection; listing/help never opens unrelated stores or providers.
_COMMANDS = {
    "snapshots": "baibai_jobs.publication.snapshot_cli",
    "model-integration": "baibai_jobs.integration.models",
    "screening-selection": "baibai_jobs.integration.screening_selection",
    "external-assessment": "baibai_jobs.acquisition.external_assessments.cli",
    "tradingview-source": "baibai_jobs.acquisition.tradingview.cli",
    "jquants-financial-native": "baibai_jobs.acquisition.jquants_financial_native",
    "jquants-price-native": "baibai_jobs.acquisition.jquants_price_native",
    "rebuild-price-native": "baibai_jobs.integration.price_native",
    "price-native-cutover": "baibai_jobs.maintenance.price_native_cutover",
    "rebuild-financial-native": "baibai_jobs.integration.financial_native",
    "financial-native-cutover": "baibai_jobs.maintenance.financial_native_cutover",
    "financial-pdf-source": "baibai_jobs.acquisition.financial_pdfs",
    "market-evidence": "baibai_jobs.acquisition.market_context",
    "article-evidence": "baibai_jobs.acquisition.article_evidence",
    "article-recovery": "baibai_jobs.maintenance.article_recovery",
    "article-cutover": "baibai_jobs.maintenance.article_cutover",
    "acquire-economic": "baibai_jobs.acquisition.economic_cli",
    "maintain-market": "baibai_jobs.maintenance.market.cli",
    "validate-market-context-stores": "baibai_jobs.observability.source_stores",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs")
    parser.add_argument("command", choices=tuple(_COMMANDS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    catalog_read = args.command == "acquire-economic" and args.arguments[:1] in (
        ["list"],
        ["groups"],
    )
    if not catalog_read and not {"-h", "--help"}.intersection(args.arguments):
        try:
            reject_noncanonical_store_paths()
        except StoreLayoutError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    command: Command = import_module(_COMMANDS[args.command]).main
    return command(args.arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
