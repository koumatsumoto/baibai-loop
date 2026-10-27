"""Unified command entry point for `baibai-engine`."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from baibai_engine.foundation.repository_layout import (
    StoreLayoutError,
    reject_noncanonical_store_paths,
)

Command = Callable[[list[str] | None], int]


class DomainModule(Protocol):
    """The shape every `baibai-engine <domain>` module exposes."""

    def build_parser(self) -> argparse.ArgumentParser: ...

    def main(self, argv: list[str] | None = None, /) -> int: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Domain:
    """One domain of the CLI, resolved on demand.

    module を遅延 import するのは、1 domain の起動に他 domain の import closure を
    読ませないためである。`--help` の一覧は summary だけで書けるので、一覧を出すために
    provider や pydantic model まで load する必要はない。
    """

    module: str
    summary: str

    def load(self) -> DomainModule:
        return cast(DomainModule, importlib.import_module(self.module))


# 各 domain の読取・書込責務を、実装のimportなしに一覧できる。
DOMAINS: Mapping[str, Domain] = {
    "external-assessment": Domain(
        module="baibai_engine.external_assessment_cli",
        summary="saved external assessments, bounded acquisition and explicit local stage import",
    ),
    "tradingview": Domain(
        module="baibai_engine.data_collection.tradingview.cli",
        summary="TradingView OAuth and analyst expectation snapshot acquisition",
    ),
    "lake": Domain(
        module="baibai_engine.storage.lake.cli",
        summary="immutable market-data manifests, object keys, and read-only inventory",
    ),
    "screening": Domain(
        module="baibai_engine.machine_analysis.screening.cli.app",
        summary="saved legacy runs and calibration diagnostics",
    ),
    "market-context": Domain(
        module="baibai_engine.machine_analysis.market_context.cli",
        summary="economic observations and mechanical market readings",
    ),
    "technical-analysis": Domain(
        module="baibai_engine.machine_analysis.technical_analysis.cli",
        summary="read-only SMA, BB, Wilder RSI and common-calendar momentum from saved prices",
    ),
    "data-collection": Domain(
        module="baibai_engine.data_collection_cli",
        summary="Data Collection: external sources and original receipts",
    ),
    "operation": Domain(
        module="baibai_engine.investment_judgment.operation.cli",
        summary="operation sessions: one active workflow at a time, with completion checks",
    ),
    "position": Domain(
        module="baibai_engine.position.cli",
        summary="portfolio ledger, human-reported broker fact drafts, and portfolio outcomes",
    ),
    "investment-judgment": Domain(
        module="baibai_engine.investment_judgment.workspace_cli",
        summary=(
            "Investment Judgment: Company Research, Stock Assessment, "
            "Independent Review, Trade Proposal"
        ),
    ),
    "task": Domain(
        module="baibai_engine.task_cli",
        summary="dated operation tasks — the canonical record of what is waiting on a date",
    ),
    "db": Domain(
        module="baibai_engine.storage.application.cli",
        summary="application database maintenance: init, backup, and schema/row inspection",
    ),
}


def _domain_table() -> str:
    width = max(len(name) for name in DOMAINS)
    lines = [f"  {name.ljust(width)}  {domain.summary}" for name, domain in DOMAINS.items()]
    return "domains:\n" + "\n".join(lines)


def _usage() -> argparse.ArgumentParser:
    # domain を subparser にしない。subparser は先頭の `--help` を自分のものとして
    # 消費するので、`baibai-engine screening --help` が domain 側の help へ届かなく
    # なる。domain の一覧は epilog で書き、引数はそのまま素通しする。
    parser = argparse.ArgumentParser(
        prog="baibai-engine",
        epilog=_domain_table(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("domain", choices=tuple(DOMAINS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _usage().parse_args(argv)
    if not {"-h", "--help"}.intersection(args.arguments):
        try:
            reject_noncanonical_store_paths()
        except StoreLayoutError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    return DOMAINS[args.domain].load().main(args.arguments)


if __name__ == "__main__":
    raise SystemExit(main())
