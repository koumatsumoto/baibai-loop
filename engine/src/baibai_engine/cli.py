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

    module を遅延 import するのは、1 domain の起動に 8 domain 分の import closure を
    読ませないためである。`--help` の一覧は summary だけで書けるので、一覧を出すために
    provider や pydantic model まで load する必要はない。
    """

    module: str
    summary: str

    def load(self) -> DomainModule:
        return cast(DomainModule, importlib.import_module(self.module))


# 各 domain が「何を読んで何を書くか」。`--help` は AGENTS.md の停止条件なので、
# domain の役割はここから 1 行で読める必要がある。
DOMAINS: Mapping[str, Domain] = {
    "tradingview": Domain(
        module="baibai_engine.market.tradingview.cli",
        summary="TradingView OAuth and analyst expectation snapshot acquisition",
    ),
    "lake": Domain(
        module="baibai_engine.market.lake.cli",
        summary="immutable market-data manifests, object keys, and read-only inventory",
    ),
    "screening": Domain(
        module="baibai_engine.screening.cli.app",
        summary="machine screening: analyze securities and publish a Review Set",
    ),
    "macro": Domain(
        module="baibai_engine.macro.indicators.cli",
        summary="macro indicator series and the macro context reports built on them",
    ),
    "operation": Domain(
        module="baibai_engine.operation.cli",
        summary="operation sessions: one active session per trigger, with completion gates",
    ),
    "position": Domain(
        module="baibai_engine.position.cli",
        summary="portfolio ledger, Position Reviews, and the drafts that record their results",
    ),
    "research": Domain(
        module="baibai_engine.research.workspace_cli",
        summary=(
            "research workspaces: thesis, Thesis Review, promotion, Capital Allocation Assessment"
        ),
    ),
    "task": Domain(
        module="baibai_engine.tasks.cli",
        summary="dated operation tasks — the canonical record of what is waiting on a date",
    ),
    "db": Domain(
        module="baibai_engine.appdb.cli",
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
