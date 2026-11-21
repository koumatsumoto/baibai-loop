from __future__ import annotations

import pytest

from baibai_engine.cli.app import main
from baibai_engine.cli.investment_judgment import build_parser as build_research_parser
from baibai_jobs.cli import main as collection_main
from baibai_jobs.maintenance.market.cli import build_parser as build_source_parser


def _choices(parser) -> set[str]:
    subparsers = next(action for action in parser._actions if action.dest == "command")
    return set(subparsers.choices)


def test_research_cli_exposes_selection_assessment_and_proposal() -> None:
    choices = _choices(build_research_parser())
    assert {
        "market-context",
        "selection-context",
        "prepare",
        "stock-assessment-scaffold",
        "independent-review-scaffold",
        "stock-assessment-evaluate",
        "stock-assessment-promote",
        "trade-proposal-context",
        "trade-proposal-check",
        "trade-proposal-publish",
        "trade-proposal-preview",
    } <= choices


@pytest.mark.parametrize(
    "domain",
    [
        "investment-judgment",
        "portfolio",
        "operation",
        "market-context",
        "technical-analysis",
    ],
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


def test_market_judgment_is_owned_by_research_cli():
    with pytest.raises(SystemExit) as result:
        main(["investment-judgment", "market-context", "--help"])
    assert result.value.code == 0


def test_source_maintenance_is_owned_by_collection():
    source = _choices(build_source_parser())
    assert {"extract-edinet-metrics", "backfill-master"} <= source
    with pytest.raises(SystemExit) as help_result:
        collection_main(["maintain-market", "--help"])
    assert help_result.value.code == 0


def test_market_snapshot_help_reaches_its_domain_owner():
    with pytest.raises(SystemExit) as result:
        main(["market-context", "snapshot", "--help"])
    assert result.value.code == 0


def test_calendar_context_read_does_not_load_judgment_or_screening():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import baibai_engine.machine_analysis.observations.prices.calendar_context as api; "
                "assert callable(api.observation_calendar_context); "
                "assert not any(name.startswith('baibai_engine.investment_judgment') "
                "or name.startswith('baibai_engine.machine_analysis.screening') "
                "for name in sys.modules)"
            ),
        ],
        check=True,
    )
