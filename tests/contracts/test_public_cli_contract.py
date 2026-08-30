from __future__ import annotations

import pytest

from baibai_engine.cli import main
from baibai_engine.research.workspace_cli import build_parser as build_research_parser
from baibai_engine.screening.cli import build_parser as build_screening_parser


def _choices(parser) -> set[str]:
    subparsers = next(action for action in parser._actions if action.dest == "command")
    return set(subparsers.choices)


def test_screening_cli_exposes_review_set_and_research_triage_only() -> None:
    choices = _choices(build_screening_parser())
    assert {"run", "review-set", "research-triage"} <= choices
    assert not ({"select", "selection", "shortlist"} & choices)


def test_review_set_capacity_is_not_a_public_cli_override() -> None:
    with pytest.raises(SystemExit):
        build_screening_parser().parse_args(
            [
                "review-set",
                "publish",
                "--asof",
                "2026-08-28",
                "--run-revision-id",
                "run-revision-fixture",
                "--review-cap",
                "20",
            ]
        )


def test_research_cli_exposes_research_set_and_capital_allocation() -> None:
    choices = _choices(build_research_parser())
    assert {"prepare", "capital-allocation-scaffold", "capital-allocation-publish"} <= choices
    assert "assessment-scaffold" not in choices


@pytest.mark.parametrize("domain", ["screening", "research", "position", "operation", "macro"])
def test_routed_domain_help_reaches_its_parser(domain: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([domain, "--help"])
    assert exit_info.value.code == 0
