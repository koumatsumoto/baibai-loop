from __future__ import annotations

import pytest

from baibai_engine.cli import main
from baibai_engine.research.workspace_cli import build_parser as build_research_parser
from baibai_engine.screening.cli.app import build_parser as build_screening_parser


def _choices(parser) -> set[str]:
    subparsers = next(action for action in parser._actions if action.dest == "command")
    return set(subparsers.choices)


def test_screening_cli_owns_single_runs_and_candidate_reads() -> None:
    choices = _choices(build_screening_parser())
    assert {"run", "run-show", "candidates"} <= choices
    assert "review-set" not in choices
    assert "research-triage" not in choices
    assert not ({"select", "selection", "shortlist"} & choices)


def test_candidate_capacity_is_not_a_public_cli_override() -> None:
    with pytest.raises(SystemExit):
        build_screening_parser().parse_args(
            [
                "candidates",
                "show",
                "--run-revision-id",
                "run-revision-fixture",
                "--review-cap",
                "20",
            ]
        )


def test_research_cli_exposes_selection_assessment_and_proposal() -> None:
    choices = _choices(build_research_parser())
    assert {
        "market-context",
        "selection-context",
        "prepare",
        "assessment-scaffold",
        "assessment-review-scaffold",
        "assessment-evaluate",
        "assessment-promote",
        "proposal-context",
        "proposal-scaffold",
        "proposal-check",
        "proposal-publish",
        "proposal-preview",
    } <= choices
    assert (
        not {
            "thesis-scaffold",
            "review-scaffold",
            "evaluate",
            "promote",
            "plan-limit",
            "capital-allocation-scaffold",
            "capital-allocation-publish",
            "triage",
            "position-prepare",
        }
        & choices
    )


@pytest.mark.parametrize(
    "domain",
    ["screening", "research", "position", "operation", "market-context", "technical-analysis"],
)
def test_routed_domain_help_reaches_its_parser(domain: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([domain, "--help"])
    assert exit_info.value.code == 0


@pytest.mark.parametrize("group", ["indicators", "reading"])
def test_market_context_group_help_reaches_its_owner(group: str) -> None:
    with pytest.raises(SystemExit) as result:
        main(["market-context", group, "--help"])
    assert result.value.code == 0


def test_runtime_has_no_retired_triage_imports_or_prepare_flags():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for directory in ("engine/src", "batch/src", "web/backend/src"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert not any(
                    name.startswith(
                        (
                            "baibai_engine.research.triage",
                            "baibai_batch.analysis",
                            "baibai_engine.research.explicit_selection",
                        )
                    )
                    for name in names
                ), path
    for flags in (
        ("--request", "old"),
        ("--ticker", "1234"),
        ("--research-triage-id", "old"),
        ("--run-revision-id", "old"),
        ("--holding-ticker", "1234"),
        ("--asof", "2026-10-14"),
    ):
        with pytest.raises(SystemExit):
            build_research_parser().parse_args(
                [
                    "prepare",
                    "--operation-id",
                    "research-op-" + "1" * 32,
                    "--workspace",
                    "workspace",
                    *flags,
                ]
            )


def test_screening_provider_protocols_do_not_import_command_implementations():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from baibai_engine.screening.cli.providers import ProviderBundle
for module in ("app", "cache", "run", "query"):
    assert f"baibai_engine.screening.cli.{module}" not in sys.modules
""",
        ],
        check=True,
    )


def test_market_judgment_is_owned_by_research_cli():
    with pytest.raises(SystemExit) as result:
        main(["research", "market-context", "--help"])
    assert result.value.code == 0
    with pytest.raises(SystemExit) as retired:
        main(["market-context", "assessment", "--help"])
    assert retired.value.code == 2
