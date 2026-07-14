from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_loop.position.cli import build_parser as position_parser
from baibai_loop.position.cli import main as position_main
from baibai_loop.screening.cli import main as screening_main
from baibai_loop.screening.cli.app import build_parser as screening_parser
from baibai_loop.thesis.decision_cli import main as decision_main
from baibai_loop.thesis.opportunity_cli import build_parser as opportunity_parser
from baibai_loop.thesis.opportunity_cli import main as opportunity_main

ROOT = Path(__file__).resolve().parents[1]
DECISION_FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
LEDGER_FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
BENCHMARK_FIXTURE = ROOT / "tests/fixtures/benchmark-observation/topix-1y.yaml"
EXECUTION_INPUT_FIXTURE = ROOT / "tests/fixtures/execution-policy/current-ladder.yaml"
HOLDING_REVIEW_FIXTURE = ROOT / "tests/fixtures/holding-review/replacement-superior.yaml"
RULES_PATH = ROOT / "records/_config/screening-rules/2026-07-06T000000+0900.yaml"


def _payload(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def test_select_cli_emits_stable_yaml_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidates_path = tmp_path / "candidates.yaml"
    candidates_path.write_text(
        yaml.safe_dump(
            {
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
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert (
        screening_main(
            [
                "select",
                "--asof",
                date(2026, 4, 24).isoformat(),
                "--candidates",
                str(candidates_path),
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

    assert set(payload) == {"recommendations", "selection"}
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


def test_ledger_cli_emits_stable_yaml_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert position_main(["ledger", "--ledger", str(LEDGER_FIXTURE)]) == 0
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
    ledger = tmp_path / "migration-ledger.yaml"
    ledger.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))

    assert position_main(["ledger", "--ledger", str(ledger)]) == 0
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


def test_outcome_cli_emits_stable_activation_pending_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    benchmark_path = tmp_path / "topix.yaml"
    benchmark_path.write_text(BENCHMARK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    assert (
        position_main(["outcome", "--root", str(tmp_path), "--benchmark-observation", "topix.yaml"])
        == 0
    )
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "schema_version",
        "kind",
        "status",
        "reason",
        "benchmark_id",
        "horizon",
    }
    assert payload["status"] == "unresolved"
    assert payload["reason"] == "activation_pending"


def test_outcome_cli_emits_stable_market_unavailable_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        position_main(
            [
                "outcome",
                "--ledger",
                str(LEDGER_FIXTURE),
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
        "benchmark_observation_ref",
        "benchmark_observation_sha256",
        "ledger_ref",
        "ledger_sha256",
        "market_data_ref",
        "market_data_sha256",
        "market_data_coverage_start_date",
        "market_data_coverage_end_date",
        "market_data_fingerprint",
    }
    assert payload["status"] == "unresolved"
    assert payload["reason"] == "benchmark_unavailable"
    assert payload["market_data_sha256"] is None


def test_decision_cli_emits_stable_yaml_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert decision_main([str(DECISION_FIXTURE)]) == 0
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "packet_status",
        "decision_readiness",
        "packet_sha256",
        "errors",
        "warnings",
        "scenarios",
        "five_year_base_break_even",
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


def test_decision_cli_emits_execution_proposal_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        decision_main(
            [
                str(DECISION_FIXTURE),
                "--execution-input",
                str(EXECUTION_INPUT_FIXTURE),
                "--ledger",
                str(LEDGER_FIXTURE),
            ],
            now=datetime.fromisoformat("2026-07-11T10:01:00+09:00"),
        )
        == 0
    )
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "packet_status",
        "decision_readiness",
        "packet_sha256",
        "errors",
        "warnings",
        "scenarios",
        "five_year_base_break_even",
        "execution_proposal",
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
    proposal = payload["execution_proposal"]
    assert isinstance(proposal, dict)
    assert set(proposal) == {
        "ticker",
        "decision_packet_sha256",
        "max_acceptable_price_yen",
        "required_5y_base_cagr_pct",
        "formula_version",
        "evaluated_at",
        "quote_observed_at",
        "ledger_as_of",
        "recommended_tactic",
        "orders",
        "cash_after_execution_yen",
        "dry_powder_after_execution_yen",
        "largest_warning",
        "detail",
    }


def test_holding_review_cli_emits_stable_yaml_shape(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "baibai_loop.position.cli.validate_holding_review_scalars",
        lambda document, root: None,
    )
    assert position_main(["holding-review", "--input", str(HOLDING_REVIEW_FIXTURE)]) == 0
    payload = _payload(capsys.readouterr().out)

    assert set(payload) == {
        "as_of",
        "position_id",
        "ticker",
        "review_status",
        "recorded_action",
        "computed_action",
        "permanent_loss_conclusion",
        "replacement",
        "valuation_review",
        "note",
    }
    replacement = payload["replacement"]
    assert isinstance(replacement, dict)
    assert set(replacement) == {
        "status",
        "edge_yen",
        "tax_basis",
        "breakeven_exit_tax_rate_bps",
        "edge_at_zero_tax_yen",
    }


def test_opportunity_cli_exposes_milestone_a_subcommands() -> None:
    parser = opportunity_parser()
    subactions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subactions) == 1
    assert set(subactions[0].choices) == {
        "prepare",
        "status",
        "packet-scaffold",
        "review-scaffold",
        "promote",
        "plan-limit",
    }


@pytest.mark.parametrize(
    "command",
    ["prepare", "status", "packet-scaffold", "review-scaffold", "promote", "plan-limit"],
)
def test_opportunity_subcommand_help_is_public(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        opportunity_main([command, "--help"])
    assert excinfo.value.code == 0


def test_opportunity_missing_required_argument_is_usage_error() -> None:
    # argparse usage errors exit 2, distinct from the data (3) / conflict (4) classes.
    with pytest.raises(SystemExit) as excinfo:
        opportunity_main(["prepare"])
    assert excinfo.value.code == 2


def test_position_cli_exposes_human_result_and_holding_build_subcommands() -> None:
    parser = position_parser()
    subactions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subactions) == 1
    assert set(subactions[0].choices) == {
        "ledger",
        "outcome",
        "holding-review",
        "holding-review-build",
        "record-result",
    }


@pytest.mark.parametrize("command", ["holding-review-build", "record-result"])
def test_position_human_boundary_subcommand_help_is_public(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        position_main([command, "--help"])
    assert excinfo.value.code == 0


@pytest.mark.parametrize(
    ("parser_factory", "argv"),
    [
        (screening_parser, ["verify-cache-coverage", "--asof", "2026-07-10"]),
        (screening_parser, ["bootstrap-cache", "--asof", "2026-07-10"]),
        (screening_parser, ["extract-edinet-metrics", "--asof", "2026-07-10"]),
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
                "--candidates",
                "/tmp/candidates.yaml",
                "--detail",
                "full",
                "--audit-top",
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
                "--ledger",
                "records/04-position/portfolio-ledger.yaml",
                "--workspace",
                ".cache/opportunity/2026-07-10",
            ],
        ),
        (opportunity_parser, ["status", "--workspace", ".cache/opportunity/2026-07-10"]),
        (
            opportunity_parser,
            [
                "packet-scaffold",
                "--workspace",
                ".cache/opportunity/2026-07-10",
                "--ticker",
                "1234",
                "--sqlite-path",
                "data/screening/market.sqlite",
                "--target-session",
                "2026-07-13",
            ],
        ),
        (
            opportunity_parser,
            [
                "review-scaffold",
                "--workspace",
                ".cache/opportunity/2026-07-10",
                "--ticker",
                "1234",
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
                "--output-dir",
                "records/03-thesis/2026/07",
            ],
        ),
        (
            opportunity_parser,
            [
                "plan-limit",
                "--packet",
                "packet.yaml",
                "--ledger",
                "ledger.yaml",
                "--sqlite-path",
                "market.sqlite",
                "--target-session",
                "2026-07-13",
                "--budget-min-yen",
                "200000",
                "--budget-max-yen",
                "300000",
                "--output",
                "proposal.yaml",
            ],
        ),
        (
            position_parser,
            [
                "record-result",
                "--ledger",
                "ledger.yaml",
                "--proposal-ref",
                "https://github.com/owner/repo/issues/1#issuecomment-1",
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
                "holding-review-build",
                "--packet",
                "packet.yaml",
                "--ledger",
                "ledger.yaml",
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
                "--root",
                ".",
                "--input",
                ".cache/holding-review/review.yaml",
            ],
        ),
    ],
)
def test_decision_cycle_runbook_recipes_use_public_cli_contract(
    parser_factory: Callable[[], argparse.ArgumentParser], argv: list[str]
) -> None:
    parser = parser_factory()
    parsed = parser.parse_args(argv)
    assert parsed.command == argv[0]
    executable = {
        screening_parser: "baibai-loop-screening",
        opportunity_parser: "baibai-loop-opportunity",
        position_parser: "baibai-loop-position",
    }[parser_factory]
    runbook = (ROOT / "docs/operations/decision-cycle.md").read_text(encoding="utf-8")
    assert f"{executable} {argv[0]}" in runbook
    for option in (item for item in argv if item.startswith("--")):
        assert option in runbook
