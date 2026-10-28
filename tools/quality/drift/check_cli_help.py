"""Reject public subcommands that `--help` cannot describe.

Public subcommands need a help description so their purpose is discoverable.
This check does not validate the correctness of the description or the operation.

argparse exposes no public API for walking a parser tree, so this gate reads the
`_SubParsersAction` internals. The alternative — running `--help` once per command path
in a subprocess and matching the printed choices block — is both slower and more brittle
than reading the objects the help text is rendered from.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

# 親 parser が argv を素通しし、実際の parser が別 module に居る箇所。stub を素通り
# させると配下の subcommand が丸ごと未検査になるので、未登録の stub は error にする。
# 表を更新し忘れた delegation は gate をすり抜けない。
DELEGATED_GROUPS: Mapping[str, str] = {
    "baibai-jobs maintain-market": "baibai_jobs.maintenance.market.cli",
    "baibai-jobs acquire-economic": "baibai_jobs.acquisition.economic_cli",
    "baibai-engine market-context snapshot": (
        "baibai_engine.cli.market_snapshot"
    ),
    "baibai-engine market-context indicators": (
        "baibai_engine.cli.economic_indicators"
    ),
    "baibai-engine market-context reading": (
        "baibai_engine.cli.market_reading"
    ),
    "baibai-engine investment-judgment market-context": (
        "baibai_engine.investment_judgment.market_assessment.cli"
    ),
    "baibai-engine investment-judgment stock-assessment-evaluate": (
        "baibai_engine.cli.stock_assessment"
    ),
    "baibai-engine investment-judgment calculate": (
        "baibai_engine.cli.assessment_calculation"
    ),
}


def build_parser(module: str) -> argparse.ArgumentParser:
    return cast(argparse.ArgumentParser, importlib.import_module(module).build_parser())


def _subparser_actions(
    parser: argparse.ArgumentParser,
) -> list[argparse._SubParsersAction]:  # type: ignore[type-arg]  # argparse ships no public parser-tree API
    return [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]


def _declares_nothing(parser: argparse.ArgumentParser) -> bool:
    """A parser with no actions parses nothing — its arguments are handled elsewhere."""
    return not parser._actions


def undescribed_subcommands(path: str, parser: argparse.ArgumentParser) -> list[str]:
    """Command paths under `parser` whose help text is missing or unreachable."""
    errors: list[str] = []
    for action in _subparser_actions(parser):
        described = {choice.dest: choice.help for choice in action._choices_actions}
        for name, declared in action.choices.items():
            command = f"{path} {name}"
            child = declared
            if not described.get(name):
                errors.append(f"{command}: subcommand has no --help description")
            if _declares_nothing(child):
                module = DELEGATED_GROUPS.get(command)
                if module is None:
                    errors.append(
                        f"{command}: delegating stub is not registered in DELEGATED_GROUPS"
                    )
                    continue
                child = build_parser(module)
            errors.extend(undescribed_subcommands(command, child))
    return errors


def check(root: Path) -> list[str]:
    del root  # the CLI surface is the installed package, not a path in the tree
    from baibai_engine.cli.app import DOMAINS

    errors: list[str] = []
    for name, domain in DOMAINS.items():
        parser = build_parser(domain.module)
        if parser.prog != f"baibai-engine {name}":
            errors.append(
                f"baibai-engine {name}: --help announces the prog {parser.prog!r}, "
                "which is not how the documented commands are invoked"
            )
        errors.extend(undescribed_subcommands(f"baibai-engine {name}", parser))
    return sorted(errors)


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
