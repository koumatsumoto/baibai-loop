from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.thesis import (
    discover_thesis_files,
    validate_thesis_collection,
    validate_thesis_file,
)

_DEFAULT_BODY = """
# Research

## 1. Thesis
text

## 2. Macro context
text

## 3. Valuation snapshot
text

## 4. 一時的割安の原因仮説
text

## 5. 反対仮説
text

## 6. Catalyst
text

## 7. Price reaction
text

## 8. Positioning / liquidity
text

## 9. Shareholder return
text

## 10. Entry 条件
text

## 11. Exit 条件
text

## 12. Invalidation
text

## 13. Position size
text
"""


def _snapshot(ref_path: str) -> dict[str, object]:
    return {
        "ref_path": ref_path,
        "effective_from": "2026-05-01T00:00:00+09:00",
    }


def _minimal_research_front_matter() -> dict[str, object]:
    """Legacy-era approved memo (published before the long-hold effective date)."""
    return {
        "ticker": "2767",
        "name": "Sample Co",
        "playbook_id": "valuation-reversion",
        "playbook_ref": _snapshot(
            "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md"
        ),
        "candidate_ref": {
            "candidates_ref": "records/02-candidates/2026/05/2026-05-01.yaml",
            "ticker": "2767",
        },
        "thesis_decision": {"outcome": "approved", "posture": "act_now"},
        "position_sizing_overlay": {
            "estimated_real_order_notional_yen": 200000,
            "adv_participation_pct": 0.1,
        },
        "thesis_payoff": {
            "max_entry_price_yen": 500,
            "fair_value_yen": 650,
            "expected_upside_pct": 30.0,
            "expected_downside_pct": 10.0,
            "risk_reward_ratio": 3.0,
            "invalidation_conditions": ["営業CF 2 期連続赤字"],
        },
        "corporate_action_check": {
            "checked": True,
            "result": "none",
        },
        "sector_33": "情報・通信業",
        "avg_turnover_oku": 2.0,
        "market_cap_oku": 100,
        "valuation": {"p_s": 0.5},
        "published_at": "2026-05-05T20:00:00+09:00",
    }


def _long_hold_front_matter() -> dict[str, object]:
    """Approved memo on/after the long-hold effective date (full new contract)."""
    front = _minimal_research_front_matter()
    front["published_at"] = "2026-07-02T20:00:00+09:00"
    payoff = front["thesis_payoff"]
    assert isinstance(payoff, dict)
    payoff["expected_yield_pct"] = 12.0
    sizing = front["position_sizing_overlay"]
    assert isinstance(sizing, dict)
    sizing["guarded_max_notional_yen"] = 200000
    front["durability_gate"] = {
        "net_cash": True,
        "operating_cf_positive": True,
        "low_leverage": True,
        "refinancing_risk": "low",
        "dividend": True,
        "judgment": "high",
    }
    front["entry_preflight"] = _entry_preflight(evaluated_on="2026-07-02")
    return front


def _entry_preflight(**overrides: object) -> dict[str, object]:
    preflight: dict[str, object] = {
        "evaluated_on": "2026-06-02",
        "market_relative_return_pct": 0.0,
        "sector_or_peer_relative_return_pct": 0.0,
        "exposure_after_order": {
            "sector_33_pct": 20.0,
            "playbook_pct": 20.0,
        },
        "action": "proceed",
        "reason": "exposure stays inside the caps",
    }
    preflight.update(overrides)
    return preflight


class ResearchValidationTests(unittest.TestCase):
    def _write(self, front_matter: object, body: str = _DEFAULT_BODY) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            front_yaml = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False)
            tmp.write(f"---\n{front_yaml}---\n{body}")
            return Path(tmp.name)

    def _findings_for(self, front_matter: object, body: str = _DEFAULT_BODY):
        path = self._write(front_matter, body=body)
        try:
            return validate_thesis_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()

    def test_minimal_valid_research_passes(self) -> None:
        findings = self._findings_for(_minimal_research_front_matter())
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_long_hold_valid_research_passes(self) -> None:
        findings = self._findings_for(_long_hold_front_matter())
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_new_approved_research_requires_entry_preflight(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-required", codes)

    def test_new_approved_research_accepts_valid_entry_preflight(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            action="starter",
            reason="entry is constrained to starter size",
        )
        findings = self._findings_for(front)
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_entry_preflight_relative_lag_is_informational_only(self) -> None:
        # 割安 (相対劣後)を買うのが本流のため、相対リターンは hard trigger にしない。
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(market_relative_return_pct=-11.9)
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_exposure_cap_blocks_proceed_until_ledger_migration(self) -> None:
        front = _long_hold_front_matter()
        front["entry_preflight"] = _entry_preflight(
            evaluated_on="2026-07-02",
            exposure_after_order={"sector_33_pct": 45.0, "playbook_pct": 20.0},
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_exposure_cap_allows_starter(self) -> None:
        front = _long_hold_front_matter()
        front["entry_preflight"] = _entry_preflight(
            evaluated_on="2026-07-02",
            exposure_after_order={"sector_33_pct": 45.0, "playbook_pct": 20.0},
            action="starter",
            reason="sector exposure is above the cap, so size down to starter",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_exposure_cap_not_applied_to_legacy_records(self) -> None:
        # legacy record の exposure % は当時の分母で記録された事実のため、cap 判定しない。
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            exposure_after_order={"sector_33_pct": 45.0, "playbook_pct": 20.0}
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_new_approved_research_uses_filename_date_for_entry_preflight_gate(self) -> None:
        front = _minimal_research_front_matter()
        del front["published_at"]
        path = self._write(front)
        named_path = path.with_name("2026-06-02-2767-valuation-reversion.md")
        path.rename(named_path)
        try:
            codes = {
                finding.code
                for finding in validate_thesis_file(
                    named_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            named_path.unlink()
        self.assertIn("thesis.entry-preflight-required", codes)

    def test_long_hold_gate_filename_date_blocks_backdated_published_at(self) -> None:
        front = _long_hold_front_matter()
        front["published_at"] = "2026-06-16T20:00:00+09:00"
        del front["durability_gate"]
        path = self._write(front)
        named = path.with_name("2026-07-02-2767-valuation-reversion.md")
        path.rename(named)
        try:
            codes = {
                f.code
                for f in validate_thesis_file(named, playbooks_root=ROOT / "records/_playbooks")
            }
        finally:
            named.unlink()
        self.assertIn("thesis.durability-gate-required", codes)

    def test_entry_preflight_rejects_invalid_evaluated_on_date(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(evaluated_on="not-a-date")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.format", codes)

    def test_entry_preflight_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        preflight = _entry_preflight()
        preflight["extra_preflight_field"] = "unexpected"
        exposure = preflight["exposure_after_order"]
        assert isinstance(exposure, dict)
        exposure["extra_exposure_field"] = 1
        front["entry_preflight"] = preflight
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_entry_preflight_rejects_unknown_action(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(action="exception")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-action", codes)

    # --- long-hold requirements (approved, on/after effective date) ---

    def test_long_hold_requires_fair_value(self) -> None:
        front = _long_hold_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        del payoff["fair_value_yen"]
        del payoff["expected_upside_pct"]
        del payoff["risk_reward_ratio"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.fair-value-required", codes)

    def test_long_hold_requires_fair_value_above_entry(self) -> None:
        front = _long_hold_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["fair_value_yen"] = 450
        payoff["expected_upside_pct"] = -10.0
        payoff["risk_reward_ratio"] = -1.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.fair-value-upside", codes)

    def test_long_hold_requires_expected_yield(self) -> None:
        front = _long_hold_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        del payoff["expected_yield_pct"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.expected-yield-required", codes)

    def test_long_hold_requires_invalidation_conditions(self) -> None:
        front = _long_hold_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["invalidation_conditions"] = []
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.invalidation-conditions-required", codes)

    def test_long_hold_requires_durability_gate(self) -> None:
        front = _long_hold_front_matter()
        del front["durability_gate"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.durability-gate-required", codes)

    def test_long_hold_requires_guarded_max_notional(self) -> None:
        front = _long_hold_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        del sizing["guarded_max_notional_yen"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.guarded-notional-required", codes)

    def test_legacy_records_are_exempt_from_long_hold_requirements(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        self.assertNotIn("expected_yield_pct", payoff)
        findings = self._findings_for(front)
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_durability_gate_rejects_unknown_judgment(self) -> None:
        front = _long_hold_front_matter()
        gate = front["durability_gate"]
        assert isinstance(gate, dict)
        gate["judgment"] = "great"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.enum", codes)

    # --- schema shape ---

    def test_legacy_macro_fields_are_rejected_by_schema(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_context_fit"] = {"context_freshness": "current"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_top_level_research_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        front["extra_top_level_field"] = "unexpected"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_position_sizing_overlay_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["paper_proxy_position_size_yen"] = 1000000
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_thesis_payoff_rejects_removed_swing_fields(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["stop_loss_yen"] = 450
        payoff["time_horizon_bd"] = 30
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_valuation_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        valuation = front["valuation"]
        assert isinstance(valuation, dict)
        valuation["extra_metric"] = 1
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    # --- decision / corporate action ---

    def test_unknown_outcome_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["thesis_decision"] = {"outcome": "maybe", "posture": "act_now"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.enum", codes)
        self.assertIn("thesis.unknown-outcome", codes)

    def test_rejected_requires_rejection_reason(self) -> None:
        front = _minimal_research_front_matter()
        front["thesis_decision"] = {"outcome": "rejected", "posture": "dropped"}
        front["position_sizing_overlay"] = {
            "estimated_real_order_notional_yen": 0,
            "adv_participation_pct": 0,
        }
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.rejection-reason-required", codes)

    def test_non_approved_requires_zero_sizing(self) -> None:
        front = _minimal_research_front_matter()
        front["thesis_decision"] = {
            "outcome": "rejected",
            "posture": "dropped",
            "rejection_reason": "thesis broken",
        }
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.rejected-sizing", codes)

    def test_approved_requires_corporate_action_check(self) -> None:
        front = _minimal_research_front_matter()
        del front["corporate_action_check"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.required", codes)
        self.assertIn("thesis.corporate-action-check", codes)

    def test_corporate_action_check_must_be_checked_for_approved_research(self) -> None:
        front = _minimal_research_front_matter()
        front["corporate_action_check"] = {"checked": False, "result": "none"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.corporate-action-check", codes)

    def test_approved_rejects_found_corporate_action(self) -> None:
        front = _minimal_research_front_matter()
        front["corporate_action_check"] = {"checked": True, "result": "found"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.corporate-action-check-result", codes)

    # --- payoff arithmetic ---

    def test_zero_entry_price_is_flagged_without_crash(self) -> None:
        # A malformed max_entry_price_yen of 0 must produce a payoff-price finding,
        # not crash the validator with ZeroDivisionError on the fair-value division.
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["max_entry_price_yen"] = 0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.payoff-price", codes)

    def test_expected_upside_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_upside_pct"] = 99.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.expected-upside", codes)

    def test_non_positive_downside_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_downside_pct"] = 0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.expected-downside", codes)

    def test_risk_reward_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["risk_reward_ratio"] = 9.9
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.risk-reward", codes)

    # --- sizing invariants ---

    def test_adv_participation_must_derive_from_order_notional(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["adv_participation_pct"] = 3.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.adv-participation-pct", codes)

    def test_order_notional_above_ticker_cap_is_flagged_until_ledger_migration(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["estimated_real_order_notional_yen"] = 700000
        sizing["adv_participation_pct"] = 0.35
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.order-notional-cap", codes)

    def test_order_notional_above_liquidity_cap_is_flagged(self) -> None:
        # liquidity cap = avg_turnover 0.1 oku x 5% = 500,000
        front = _minimal_research_front_matter()
        front["avg_turnover_oku"] = 0.1
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["estimated_real_order_notional_yen"] = 550000
        sizing["adv_participation_pct"] = 5.5
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.order-notional-cap", codes)

    def test_approved_requires_avg_turnover(self) -> None:
        front = _minimal_research_front_matter()
        del front["avg_turnover_oku"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.adv-participation-input", codes)

    # --- basics ---

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["ticker"] = "bad"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.ticker-format", codes)

    def test_unknown_playbook_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook_id"] = "unknown-playbook"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.unknown-playbook", codes)

    def test_missing_required_section_is_flagged(self) -> None:
        body = _DEFAULT_BODY.replace("## 6. Catalyst\ntext\n", "")
        codes = {
            finding.code for finding in self._findings_for(_minimal_research_front_matter(), body)
        }
        self.assertIn("thesis.missing-section", codes)

    def test_no_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("# Research\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_thesis_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("thesis.no-front-matter", codes)

    def test_front_matter_non_mapping_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\n- not\n- mapping\n---\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_thesis_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("thesis.front-matter-non-mapping", codes)

    def test_missing_playbook_schema_at_load_time_is_handled(self) -> None:
        front = _minimal_research_front_matter()
        with patch(
            "baibai_loop.thesis.core.load_playbook_schema",
            side_effect=FileNotFoundError("missing"),
        ):
            codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.playbook-schema", codes)

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\nfoo: [\n---\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_thesis_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("thesis.invalid-yaml", codes)

    def test_repository_research_files_pass_without_errors(self) -> None:
        repo_research = ROOT / "records/03-thesis"
        files = discover_thesis_files(repo_research)
        if not files:
            self.skipTest("no research files under repository root")
        for path in files:
            findings = validate_thesis_file(path, playbooks_root=ROOT / "records/_playbooks")
            errors = [finding for finding in findings if finding.severity == "error"]
            self.assertEqual(errors, [], f"research markdown {path} produced errors: {errors}")

    def test_sector_concentration_warns_for_three_approved_memos(self) -> None:
        fronts = []
        for ticker in ("1111", "2222", "3333"):
            front = _minimal_research_front_matter()
            front["ticker"] = ticker
            fronts.append((Path(f"{ticker}.md"), front))

        findings = validate_thesis_collection(fronts)

        self.assertEqual(len(findings), 3)
        self.assertTrue(all(finding.code == "thesis.sector-concentration" for finding in findings))


if __name__ == "__main__":
    unittest.main()
