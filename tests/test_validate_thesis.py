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
    return {
        "ticker": "2767",
        "name": "Sample Co",
        "playbook_id": "valuation-reversion",
        "playbook_ref": _snapshot(
            "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md"
        ),
        "candidate_ref": {
            "candidates_ref": "records/04-candidates/2026/05/2026-05-01.yaml",
            "ticker": "2767",
        },
        "thesis_decision": {"outcome": "approved", "posture": "act_now"},
        "macro_context_ref": (
            "records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml"
        ),
        "macro_context_fit": {
            "context_freshness": "current",
            "fit": "neutral",
            "decision_effect": "proceed",
            "required_checks": [],
            "sizing_caution": [],
        },
        "position_sizing_overlay": {
            "paper_proxy_position_size_yen": 1000000,
            "real_order_intent_yen": 200000,
            "adv_participation_pct": 0.5,
        },
        "thesis_payoff": {
            "max_entry_price_yen": 500,
            "target_price_yen": 650,
            "stop_loss_yen": 450,
            "expected_upside_pct": 30.0,
            "expected_downside_pct": 10.0,
            "risk_reward_ratio": 3.0,
            "time_horizon_bd": 30,
            "invalidation_conditions": ["stop loss"],
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


def _entry_preflight(**overrides: object) -> dict[str, object]:
    preflight: dict[str, object] = {
        "evaluated_on": "2026-06-02",
        "market_relative_return_pct": 0.0,
        "sector_or_peer_relative_return_pct": 0.0,
        "macro_freshness": "current",
        "tactical_exposure_after_order": {
            "sector_33_pct": 20.0,
            "playbook_pct": 20.0,
        },
        "near_term_catalyst": False,
        "action": "proceed",
        "reason": "relative performance and exposure are acceptable",
    }
    preflight.update(overrides)
    return preflight


def _write_macro_context_fixture(root: Path) -> None:
    macro_path = root / "records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml"
    macro_path.parent.mkdir(parents=True)
    macro_path.write_text(
        yaml.safe_dump(
            {
                "kind": "macro-context",
                "context_id": "macro-context-2026-05-04-screening",
                "as_of": "2026-05-04",
                "valid_until": "2026-05-17",
                "published_at": "2026-05-04T20:00:00+09:00",
                "summary": "test",
                "inputs": {"articles": [], "indicator_series": []},
                "sector_tilts": {
                    "items": [
                        {
                            "id": "info-neutral",
                            "scope": "sector_33",
                            "key": "情報・通信業",
                            "stance": "neutral",
                            "strength": "medium",
                            "confidence": "medium",
                            "rationale": "test",
                        }
                    ]
                },
                "research_questions": ["question"],
                "refresh_triggers": ["trigger"],
                "changes_since_previous": [],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_playbook_ref_fixture(root: Path) -> None:
    path = root / "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nplaybook_id: valuation-reversion\n---\n# Playbook\n", encoding="utf-8")


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

    def test_new_approved_research_requires_entry_preflight(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-required", codes)

    def test_new_approved_research_accepts_valid_entry_preflight(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        fit = front["macro_context_fit"]
        assert isinstance(fit, dict)
        fit["context_freshness"] = "stale"
        front["entry_preflight"] = _entry_preflight(
            macro_freshness="stale",
            action="starter",
            reason="macro is stale, so entry is constrained to starter size",
        )
        findings = self._findings_for(front)
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_entry_preflight_rejects_reasonless_proceed_with_relative_lag(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(market_relative_return_pct=-3.1)
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_rejects_stale_macro_proceed(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        fit = front["macro_context_fit"]
        assert isinstance(fit, dict)
        fit["context_freshness"] = "stale"
        front["entry_preflight"] = _entry_preflight(macro_freshness="stale")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_rejects_high_tactical_exposure_proceed(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            tactical_exposure_after_order={"sector_33_pct": 51.0, "playbook_pct": 20.0}
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_exception_requires_structured_basis(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_relative_return_pct=-3.1,
            action="exception",
            reason="exception is justified",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-exception-basis", codes)

    def test_entry_preflight_exception_accepts_near_term_catalyst_basis(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        fit = front["macro_context_fit"]
        assert isinstance(fit, dict)
        fit["context_freshness"] = "stale"
        front["entry_preflight"] = _entry_preflight(
            market_relative_return_pct=-3.1,
            macro_freshness="stale",
            near_term_catalyst=True,
            action="exception",
            exception_basis=["near_term_catalyst"],
            reason="near-term catalyst can reprice the lag quickly",
        )
        errors = [finding for finding in self._findings_for(front) if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_entry_preflight_rejects_proceed_in_risk_on_rally(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="proceed",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_warns_starter_contrarian_in_risk_on_rally(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="starter",
            reason="starter size against a rally without a catalyst",
        )
        findings = self._findings_for(front)
        codes = {finding.code for finding in findings}
        self.assertNotIn("thesis.entry-preflight-proceed-trigger", codes)
        self.assertIn("thesis.entry-preflight-rally-contrarian", codes)

    def test_entry_preflight_allows_starter_in_rally_with_near_term_catalyst(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="starter",
            near_term_catalyst=True,
            reason="near-term catalyst can trigger the mean reversion",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-rally-contrarian", codes)

    def test_entry_preflight_allows_rally_entry_with_low_correlation_basis(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="exception",
            exception_basis=["low_correlation"],
            reason="low correlation to the rally leaders dilutes the regime bet",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-rally-contrarian", codes)

    def test_entry_preflight_neutral_range_proceed_has_no_regime_finding(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "neutral_range", "benchmark_return_20d": 0.01},
            action="proceed",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-proceed-trigger", codes)
        self.assertNotIn("thesis.entry-preflight-rally-contrarian", codes)

    def test_entry_preflight_requires_market_regime_from_gate_date(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-17T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(action="defer")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-regime-required", codes)

    def test_entry_preflight_rejects_unknown_regime_label(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        preflight = _entry_preflight(action="defer")
        preflight["market_regime"] = {"regime": "melt_up", "benchmark_return_20d": 0.08}
        front["entry_preflight"] = preflight
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-regime-label", codes)

    def test_entry_preflight_rejects_proceed_with_unknown_regime(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-17T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "unknown", "benchmark_return_20d": None},
            action="proceed",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-proceed-trigger", codes)

    def test_entry_preflight_rejects_mislabeled_regime_against_benchmark(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "neutral_range", "benchmark_return_20d": 0.10},
            action="proceed",
        )
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-regime-mismatch", codes)

    def test_entry_preflight_rejects_partial_market_regime_block(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-17T20:00:00+09:00"
        preflight = _entry_preflight(action="defer")
        # regime key missing — schema also catches this, but the custom check
        # must also flag it so future schema loosenings don't open a hole.
        preflight["market_regime"] = {"benchmark_return_20d": 0.05}
        front["entry_preflight"] = preflight
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.entry-preflight-regime-required", codes)

    def test_entry_preflight_published_2026_06_16_does_not_require_market_regime(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-16T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(action="defer")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-regime-required", codes)

    def test_entry_preflight_filename_date_blocks_backdated_published_at(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-16T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(action="defer")
        path = self._write(front)
        named = path.with_name("2026-06-20-2767-valuation-reversion.md")
        path.rename(named)
        try:
            codes = {
                f.code
                for f in validate_thesis_file(named, playbooks_root=ROOT / "records/_playbooks")
            }
        finally:
            named.unlink()
        self.assertIn("thesis.entry-preflight-regime-required", codes)

    def test_entry_preflight_defer_in_rally_has_no_warning(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="defer",
        )
        codes = {f.code for f in self._findings_for(front)}
        self.assertNotIn("thesis.entry-preflight-rally-contrarian", codes)

    def test_entry_preflight_exception_with_only_low_sizing_fails_in_rally(self) -> None:
        front = _minimal_research_front_matter()
        front["published_at"] = "2026-06-02T20:00:00+09:00"
        front["entry_preflight"] = _entry_preflight(
            market_regime={"regime": "risk_on_rally", "benchmark_return_20d": 0.08},
            action="exception",
            exception_basis=["low_sizing"],
            tactical_exposure_after_order={"sector_33_pct": 10.0, "playbook_pct": 10.0},
            reason="low_sizing only is insufficient in rally per backtest",
        )
        findings = self._findings_for(front)
        codes = {f.code for f in findings}
        rally_finding = [f for f in findings if f.code == "thesis.entry-preflight-rally-contrarian"]
        self.assertIn("thesis.entry-preflight-rally-contrarian", codes)
        self.assertEqual(
            [f.severity for f in rally_finding],
            ["error"],
            f"expected error severity for exception, got {rally_finding}",
        )

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
        exposure = preflight["tactical_exposure_after_order"]
        assert isinstance(exposure, dict)
        exposure["extra_exposure_field"] = 1
        front["entry_preflight"] = preflight
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_missing_required_field_is_flagged_by_schema(self) -> None:
        front = _minimal_research_front_matter()
        del front["macro_context_fit"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.required", codes)

    def test_top_level_research_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        front["extra_top_level_field"] = "unexpected"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_position_sizing_overlay_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["extra_size_field"] = 1
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

    def test_valuation_rejects_unknown_field(self) -> None:
        front = _minimal_research_front_matter()
        valuation = front["valuation"]
        assert isinstance(valuation, dict)
        valuation["extra_metric"] = 1
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.additionalProperties", codes)

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
            "paper_proxy_position_size_yen": 0,
            "real_order_intent_yen": 0,
            "adv_participation_pct": 0,
        }
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.rejection-reason-required", codes)

    def test_deferred_macro_context_cannot_be_approved(self) -> None:
        front = _minimal_research_front_matter()
        fit = front["macro_context_fit"]
        assert isinstance(fit, dict)
        fit["decision_effect"] = "defer"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.macro-context-defer-approved", codes)

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

    def test_payoff_order_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["stop_loss_yen"] = 700
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.payoff-order", codes)

    def test_zero_entry_price_is_flagged_without_crash(self) -> None:
        # A malformed max_entry_price_yen of 0 must produce a payoff-order finding,
        # not crash the validator with ZeroDivisionError on the target/entry division.
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["max_entry_price_yen"] = 0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.payoff-order", codes)

    def test_expected_upside_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_upside_pct"] = 99.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("thesis.expected-upside", codes)

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
        repo_research = ROOT / "records/05-thesis"
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
