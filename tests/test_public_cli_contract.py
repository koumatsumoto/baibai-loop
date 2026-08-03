from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_engine.macro.indicators.cli import build_parser as macro_parser
from baibai_engine.macro.indicators.cli import main as macro_main
from baibai_engine.position.cli import build_parser as position_parser
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.ledger import PortfolioLedgerDocument, load_portfolio_ledger
from baibai_engine.position.outcome_store import PortfolioOutcomeStore
from baibai_engine.proposals.cli import build_parser as proposal_parser
from baibai_engine.research.decision_cli import main as decision_main
from baibai_engine.research.opportunity_cli import build_parser as opportunity_parser
from baibai_engine.research.opportunity_cli import main as opportunity_main
from baibai_engine.screening.cli import main as screening_main
from baibai_engine.screening.cli.app import build_parser as screening_parser
from baibai_engine.screening.run_store import ScreeningRunStore
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW

ROOT = Path(__file__).resolve().parents[1]
DECISION_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
LEDGER_FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
BENCHMARK_FIXTURE = ROOT / "tests/fixtures/benchmark-observation/topix-1y.yaml"
RULES_PATH = ROOT / "method/screening-rules/2026-07-06T000000+0900.yaml"


def _payload(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def _import_ledger(db_path: Path, source: Path = LEDGER_FIXTURE) -> None:
    document = load_portfolio_ledger(source)
    seed_ledger(
        db_path,
        document.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in document.market_prices
                )
            }
        ),
    )


def test_select_cli_emits_stable_yaml_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_db = tmp_path / "runs.sqlite"
    run_revision_id = "run-revision-public-contract"
    ScreeningRunStore(runs_db).publish_run(
        {
            "run_id": "screening-20260424",
            "run_date": "2026-04-24",
            "asof_date": "2026-04-24",
            "run_at": "2026-04-24T18:00:00+09:00",
            "universe_size": 1,
            "rules_ref": str(RULES_PATH),
            "candidates": [
                {
                    "ticker": "1111",
                    "name": "contract candidate",
                    "sector_33": "機械",
                    "market_cap_oku": 300,
                    "avg_turnover_oku": 2.0,
                    "listing_span_days": 1200,
                    "jpx_flags": [],
                    "metrics": {"er_annual": 1.0},
                    "evidence_hits": [
                        {
                            "name": "valuation-reversion",
                            "playbook_id": "cashflow-yield-discount",
                            "source_status": "ok",
                            "sizing_eligible": True,
                        }
                    ],
                }
            ],
        },
        run_revision_id=run_revision_id,
    )

    assert (
        screening_main(
            [
                "select",
                "--asof",
                date(2026, 4, 24).isoformat(),
                "--run-revision-id",
                run_revision_id,
                "--runs-db",
                str(runs_db),
                "--top",
                "1",
                "--rules-path",
                str(RULES_PATH),
                "--sqlite-path",
                str(tmp_path / "missing-market.sqlite"),
            ]
        )
        == 0
    )
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {"recommendations", "selection", "selection_id"}
    assert str(payload["selection_id"]).startswith("selection-")
    recommendations = payload["recommendations"]
    assert isinstance(recommendations, list)
    assert len(recommendations) == 1
    assert set(recommendations[0]) == {
        "rank",
        "ticker",
        "name",
        "sector_33",
        "selection_playbook",
        "market_cap_oku",
        "avg_turnover_oku",
        "per_trailing",
        "per_forward",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "cash_to_market_cap",
        "net_cash_to_market_cap",
        "investment_securities",
        "asset_backed_ratio",
        "equity_ratio",
        "ocf_yield",
        "operating_profit_yoy",
        "sales_yoy",
        "fcf_yield",
        "er_annual",
        "er_reversion_annual",
        "er_carry_annual",
        "er_dividend_yield",
        "er_anchor_metrics",
        "er_origin",
        "er_model_version",
        "er_unit",
        "er_assumptions",
        "fv_sector_median_yen",
        "fv_self_range_yen",
        "dps_actual_annual",
        "dps_forecast_annual",
        "dividend_yield",
        "price_change_5d",
        "price_change_20d",
        "price_change_60d",
        "benchmark_relative_20d",
        "gap_from_52w_low",
        "price_history_coverage_750d",
        "split_adjustment_flag",
        "next_earnings_date",
        "position_tier",
        "durability_rating",
        "durability_caution_reasons",
        "previous_candidate",
        "reason_tags",
        "risk_tags",
        "decision_input_seed",
    }
    selection = payload["selection"]
    assert isinstance(selection, dict)
    assert set(selection) == {
        "asof",
        "profile",
        "input_refs",
        "counts",
        "research_selection_target_max",
        "research_selection_playbook_order",
        "macro_context_summary",
        "diagnostics",
        "detail",
    }
    input_refs = selection["input_refs"]
    assert isinstance(input_refs, dict)
    assert set(input_refs) == {
        "candidates_ref",
        "macro_context_ref",
        "previous_candidates_ref",
    }


def test_ledger_cli_emits_stable_yaml_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "app.sqlite"
    _import_ledger(db_path)
    assert position_main(["ledger", "--db", str(db_path)]) == 0
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "as_of",
        "portfolio_scope",
        "available_cash_yen",
        "reserved_cash_yen",
        "deployed_cost_yen",
        "holdings_market_value_yen",
        "confirmed_income_yen",
        "confirmed_cost_yen",
        "confirmed_tax_yen",
        "confirmed_cost_tax_yen",
        "book_capital_yen",
        "total_capital_yen",
        "estimated_exit_tax_rate_bps",
        "estimated_exit_tax_basis",
        "estimated_exit_tax_yen",
        "holdings",
        "active_reservations",
        "warnings",
        "event_annotations",
    }
    assert payload["event_annotations"] == []
    holdings = payload["holdings"]
    assert isinstance(holdings, list)
    assert holdings
    assert set(holdings[0]) == {
        "ticker",
        "sector",
        "common_factors",
        "quantity",
        "deployed_cost_yen",
        "market_price_yen",
        "market_price_observed_at",
        "market_price_source_kind",
        "market_price_basis",
        "market_price_source_ref",
        "market_value_yen",
    }


def test_ledger_cli_labels_migration_events_as_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = yaml.safe_load(LEDGER_FIXTURE.read_text())
    assert isinstance(raw, dict)
    events = raw["events"]
    assert isinstance(events, list)
    events[0]["event_id"] = "migration-opening-20260501"
    document = PortfolioLedgerDocument.model_validate(raw)
    document = document.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in document.market_prices
            )
        }
    )
    db_path = tmp_path / "app.sqlite"
    seed_ledger(db_path, document)

    assert position_main(["ledger", "--db", str(db_path)]) == 0
    payload = _payload(capsys.readouterr().out)

    assert payload["event_annotations"] == [
        {
            "code": "ledger.migration-initialization",
            "event_count": 1,
            "message": (
                "migration events initialize canonical state and are not "
                "human-reported broker results"
            ),
        }
    ]


def test_outcome_cli_emits_stable_market_unavailable_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "app.sqlite"
    _import_ledger(db_path)
    assert (
        position_main(
            [
                "outcome",
                "--db",
                str(db_path),
                "--benchmark-observation",
                str(BENCHMARK_FIXTURE),
                "--sqlite",
                str(tmp_path / "missing-market.sqlite"),
            ]
        )
        == 0
    )
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "schema_version",
        "kind",
        "status",
        "reason",
        "horizon",
        "period_start_date",
        "period_end_date",
        "benchmark_id",
        "benchmark_observation",
        "market_data_ref",
        "market_data_sha256",
        "market_data_coverage_start_date",
        "market_data_coverage_end_date",
        "market_data_fingerprint",
    }
    assert payload["status"] == "unresolved"
    assert payload["reason"] == "benchmark_unavailable"
    assert payload["market_data_sha256"] is None
    assert PortfolioOutcomeStore(db_path).list() == ()


def test_decision_cli_emits_stable_yaml_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert decision_main([str(DECISION_FIXTURE)], now=FIXED_NOW) == 0
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "thesis_status",
        "decision_readiness",
        "thesis_sha256",
        "errors",
        "warnings",
        "scenarios",
        "five_year_base_break_even",
        "screening_fv_revision_pct",
    }
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert scenarios
    assert set(scenarios[0]) == {
        "horizon_years",
        "name",
        "terminal_earnings_yen",
        "terminal_share_count",
        "terminal_price_yen",
        "total_return_cagr_pct",
    }
    break_even = payload["five_year_base_break_even"]
    assert isinstance(break_even, dict)
    assert set(break_even) == {
        "required_total_value_yen",
        "required_total_return_cagr_pct",
        "base_terminal_valuation_multiple",
        "break_even_terminal_valuation_multiple",
        "terminal_multiple_downside_buffer",
        "terminal_multiple_status",
        "base_annual_earnings_growth_pct",
        "break_even_annual_earnings_growth_pct",
        "earnings_growth_downside_buffer_pct_points",
        "earnings_growth_status",
        "observed_trailing_multiple_status",
        "observed_trailing_multiple_fact_id",
        "observed_trailing_multiple",
        "base_terminal_multiple_minus_observed",
        "base_terminal_multiple_premium_pct",
    }


def test_decision_cli_emits_the_thesis_evaluation_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert decision_main([str(DECISION_FIXTURE)], now=FIXED_NOW) == 0
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "thesis_status",
        "decision_readiness",
        "thesis_sha256",
        "errors",
        "warnings",
        "scenarios",
        "five_year_base_break_even",
        "screening_fv_revision_pct",
    }
    break_even = payload["five_year_base_break_even"]
    assert isinstance(break_even, dict)
    assert set(break_even) == {
        "required_total_value_yen",
        "required_total_return_cagr_pct",
        "base_terminal_valuation_multiple",
        "break_even_terminal_valuation_multiple",
        "terminal_multiple_downside_buffer",
        "terminal_multiple_status",
        "base_annual_earnings_growth_pct",
        "break_even_annual_earnings_growth_pct",
        "earnings_growth_downside_buffer_pct_points",
        "earnings_growth_status",
        "observed_trailing_multiple_status",
        "observed_trailing_multiple_fact_id",
        "observed_trailing_multiple",
        "base_terminal_multiple_minus_observed",
        "base_terminal_multiple_premium_pct",
    }


def test_opportunity_cli_exposes_the_research_authoring_subcommands() -> None:
    parser = opportunity_parser()
    subactions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subactions) == 1
    assert set(subactions[0].choices) == {
        "prepare",
        "holding-prepare",
        "status",
        "thesis-scaffold",
        "review-scaffold",
        "evaluate",
        "promote",
        "plan-limit",
        "assessment-scaffold",
        "assessment-publish",
    }


def test_macro_cli_lists_every_command_group() -> None:
    # `--help` is the operator's source of truth for what a domain accepts, so the
    # groups routed to another module are listed alongside the series commands.
    parser = macro_parser()
    subactions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subactions) == 1
    assert set(subactions[0].choices) == {
        "context",
        "reading",
        "list",
        "search",
        "get",
        "refresh",
        "retract",
    }


@pytest.mark.parametrize(
    "command",
    [
        "prepare",
        "holding-prepare",
        "status",
        "thesis-scaffold",
        "review-scaffold",
        "promote",
        "plan-limit",
    ],
)
def test_opportunity_subcommand_help_is_public(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        opportunity_main([command, "--help"])
    assert excinfo.value.code == 0


@pytest.mark.parametrize(
    ("entry", "command", "prog"),
    [
        (macro_main, "context", "baibai-engine macro context"),
        (macro_main, "reading", "baibai-engine macro reading"),
        (opportunity_main, "evaluate", "baibai-engine research evaluate"),
    ],
)
def test_routed_command_help_reaches_its_own_parser(
    entry: Callable[[list[str]], int],
    command: str,
    prog: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`<domain> <command> --help` must render the routed module's own usage.

    The listing in the domain parser is a stub, so a hand-off that went through
    argparse instead of around it would answer with the domain usage or a usage
    error rather than the command's own options.
    """
    with pytest.raises(SystemExit) as excinfo:
        entry([command, "--help"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: {prog}")


def test_opportunity_missing_required_argument_is_usage_error() -> None:
    # argparse usage errors exit 2, distinct from the data (3) / conflict (4) classes.
    with pytest.raises(SystemExit) as excinfo:
        opportunity_main(["prepare"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("entry", "command"),
    [(macro_main, "bogus"), (opportunity_main, "bogus")],
)
def test_unknown_domain_command_is_rejected(
    entry: Callable[[list[str]], int], command: str
) -> None:
    # Routing ahead of argparse must not turn an unknown command into a hand-off.
    with pytest.raises(SystemExit) as excinfo:
        entry([command])
    assert excinfo.value.code == 2


def test_position_cli_exposes_human_result_and_holding_build_subcommands() -> None:
    parser = position_parser()
    subactions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subactions) == 1
    assert set(subactions[0].choices) == {
        "ledger",
        "apply-draft",
        "event-draft",
        "meta-draft",
        "override-draft",
        "outcome",
        "holding-review",
        "holding-review-build",
        "market-price-draft",
        "record-result",
        "sell-result-draft",
    }


@pytest.mark.parametrize(
    "command",
    ["holding-review-build", "market-price-draft", "record-result", "sell-result-draft"],
)
def test_position_human_boundary_subcommand_help_is_public(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        position_main([command, "--help"])
    assert excinfo.value.code == 0


@pytest.mark.parametrize(
    "parser_factory",
    [opportunity_parser, position_parser, proposal_parser],
)
def test_current_decision_clis_do_not_expose_backdated_clock(
    parser_factory: Callable[[], argparse.ArgumentParser],
) -> None:
    pending = [parser_factory()]
    option_strings: set[str] = set()
    while pending:
        parser = pending.pop()
        for action in parser._actions:
            option_strings.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())

    assert "--now" not in option_strings


@pytest.mark.parametrize(
    ("parser_factory", "argv"),
    [
        (
            screening_parser,
            ["run", "--asof", "2026-07-10", "--output-path", "/tmp/candidates.yaml"],
        ),
        (
            screening_parser,
            [
                "select",
                "--asof",
                "2026-07-10",
                "--run-revision-id",
                "run-revision-example",
                "--longlist-top",
                "20",
                "--output-path",
                "/tmp/selection.yaml",
            ],
        ),
        (
            opportunity_parser,
            [
                "prepare",
                "--asof",
                "2026-07-10",
                "--selection-output",
                "/tmp/selection.yaml",
                "--db",
                "data/app/baibai.sqlite",
                "--workspace",
                ".cache/opportunity/2026-07-10",
            ],
        ),
        (
            opportunity_parser,
            [
                "promote",
                "--workspace",
                ".cache/opportunity/2026-07-10",
                "--ticker",
                "1234",
                "--db",
                "data/app/baibai.sqlite",
            ],
        ),
        (
            opportunity_parser,
            [
                "plan-limit",
                "--thesis",
                "thesis.yaml",
                "--db",
                "data/app/baibai.sqlite",
                "--sqlite-path",
                "market.sqlite",
                "--target-session",
                "2026-07-13",
                "--output",
                "proposal.yaml",
            ],
        ),
        (
            position_parser,
            [
                "market-price-draft",
                "--db",
                "data/app/baibai.sqlite",
                "--sqlite",
                "data/screening/market.sqlite",
                "--asof",
                "2026-07-10",
                "--out",
                ".cache/position/2026-07-10-market-price-ledger.yaml",
            ],
        ),
        (
            position_parser,
            [
                "record-result",
                "--db",
                "data/app/baibai.sqlite",
                "--proposal-ref",
                "prop-20260712-1234-example",
                "--status",
                "filled",
                "--occurred-at",
                "2026-07-12T10:00:00+09:00",
                "--ticker",
                "1234",
                "--quantity",
                "100",
                "--price-yen",
                "990",
                "--reservation-id",
                "reservation-1",
                "--out",
                ".cache/ledger/draft.yaml",
            ],
        ),
        (
            position_parser,
            [
                "sell-result-draft",
                "--db",
                "data/app/baibai.sqlite",
                "--ticker",
                "1234",
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--occurred-at",
                "2026-07-12T10:00:00+09:00",
                "--decision-reference",
                "holding-review-20260712-1234-position-1",
                "--out",
                ".cache/ledger/sell-draft.yaml",
            ],
        ),
        (
            position_parser,
            [
                "holding-review-build",
                "--db",
                "data/app/baibai.sqlite",
                "--thesis-id",
                "thesis-20260712-1234-r1",
                "--position-id",
                "position-1",
                "--out",
                ".cache/holding-review/review.yaml",
            ],
        ),
        (
            position_parser,
            [
                "holding-review",
                "--input",
                ".cache/holding-review/review.yaml",
            ],
        ),
    ],
)
def test_skill_recipes_use_public_cli_contract(
    parser_factory: Callable[[], argparse.ArgumentParser], argv: list[str]
) -> None:
    parser = parser_factory()
    parsed = parser.parse_args(argv)
    assert parsed.command == argv[0]
    executable = {
        screening_parser: "baibai-engine screening",
        opportunity_parser: "baibai-engine research",
        position_parser: "baibai-engine position",
    }[parser_factory]
    skills = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
    )
    assert f"{executable} {argv[0]}" in skills
