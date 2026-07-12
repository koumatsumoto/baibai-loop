from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_loop.position.cli import main as position_main
from baibai_loop.screening.cli import main as screening_main
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
    }
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
        "execution_proposal",
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


def test_holding_review_cli_emits_stable_yaml_shape(capsys: pytest.CaptureFixture[str]) -> None:
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
