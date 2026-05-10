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

from baibai_loop.validate.research import (
    discover_research_files,
    validate_research_collection,
    validate_research_file,
)

_DIGEST = "sha256:" + "a" * 64

_DEFAULT_BODY = """
# Research

## 1. Thesis
text

## 2. Macro regime gate
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


def _snapshot(ref_path: str, digest: str = _DIGEST) -> dict[str, object]:
    return {
        "ref_path": ref_path,
        "content_sha256": digest,
        "effective_from": "2026-05-01T00:00:00+09:00",
    }


def _calendar_snapshots() -> dict[str, object]:
    return {
        "business_days": _snapshot("records/_calendars/business-days/2026-05.yaml"),
        "events": _snapshot("records/_calendars/events/2026-05.yaml"),
        "corporate_actions": _snapshot("records/_calendars/corporate-actions/2026-05.yaml"),
    }


def _minimal_research_front_matter() -> dict[str, object]:
    return {
        "ticker": "2767",
        "name": "Sample Co",
        "playbook_id": "valuation-reversion",
        "playbook_snapshot": _snapshot(
            "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md"
        ),
        "policy_snapshot": _snapshot(
            "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
        ),
        "portfolio_exposure_snapshot_ref": _snapshot(
            "records/_portfolio-exposure/2026/05/2026-05-05T133000+0900.yaml"
        ),
        "policy_applicability": "active",
        "calendars_snapshot": _calendar_snapshots(),
        "candidate_ref": {
            "candidates_ref": "records/04-candidates/2026/05/2026-05-01.yaml",
            "screen_run_id": "screening-20260501",
            "ticker": "2767",
            "candidate_id": "candidate-2026-05-01-2767",
        },
        "research_decision": {"outcome": "approved", "posture": "act_now"},
        "macro_regime_gate": {
            "aggregate_status": "supportive",
            "decision_effect": "pass",
            "inputs": [
                {
                    "scope": "sector",
                    "key": "情報・通信業",
                    "status": "supportive",
                    "source_ref": (
                        "records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml"
                    ),
                }
            ],
        },
        "candidate_evidence_decisions": [
            {
                "evidence_hit_id": "eh-1",
                "effective_sizing_eligible": True,
                "evaluated_at": "2026-05-05T20:00:00+09:00",
                "reason_code": "source_status_ok",
                "independence_component_id": "component-1",
            }
        ],
        "selected_supporting_evidence_refs": [{"source": "candidate", "evidence_hit_id": "eh-1"}],
        "research_evidence_hits": [
            {
                "evidence_hit_id": "risk-1",
                "decision_role": "risk_evidence",
                "evidence_polarity": "risk",
            }
        ],
        "independent_evidence_count": 1,
        "conviction_tier": "medium",
        "conviction_tier_path": "count_breadth",
        "position_sizing_overlay": {
            "paper_proxy_position_size_yen": 1000000,
            "real_order_intent_yen": 50000,
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
        "sector_33": "情報・通信業",
        "avg_turnover_oku": 2.0,
        "ai-draft": True,
        "published_at": "2026-05-05T20:00:00+09:00",
    }


def _write_candidate_fixture(root: Path) -> None:
    candidates_path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "screening-20260501",
                "candidates": [
                    {
                        "ticker": "2767",
                        "candidate_id": "candidate-2026-05-01-2767",
                        "screen_run_id": "screening-20260501",
                        "sector_33": "情報・通信業",
                        "avg_turnover_oku": 2.0,
                        "market_cap_oku": 100,
                        "metrics": {"p_s": 0.5},
                        "evidence_hits": [
                            {
                                "evidence_hit_id": "eh-1",
                                "playbook_id": "valuation-reversion",
                                "evidence_family_set": ["valuation"],
                                "independence_component_id": "component-1",
                            }
                        ],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


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
            return validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()

    def test_minimal_valid_research_passes(self) -> None:
        findings = self._findings_for(_minimal_research_front_matter())
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_missing_required_field_is_flagged_by_schema(self) -> None:
        front = _minimal_research_front_matter()
        del front["macro_regime_gate"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.required", codes)

    def test_removed_front_matter_field_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["_".join(("macro", "gate"))] = "neutral"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.removed-field", codes)

    def test_missing_policy_applicability_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["policy_applicability"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.policy-applicability", codes)

    def test_missing_calendars_snapshot_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["calendars_snapshot"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.calendars-snapshot", codes)

    def test_unknown_outcome_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "maybe", "posture": "act_now"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.enum", codes)
        self.assertIn("research.unknown-outcome", codes)

    def test_rejected_requires_rejection_reason(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "rejected", "posture": "dropped"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.rejection-reason-required", codes)

    def test_deferred_requires_deferral_reason(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "deferred", "posture": "wait_for_event"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.deferral-reason-required", codes)

    def test_blocked_macro_regime_gate_cannot_be_approved(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_regime_gate"] = {"decision_effect": "block"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.blocked-gate-approved", codes)

    def test_unknown_macro_regime_gate_effect_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_regime_gate"] = {"decision_effect": "wrong"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.unknown-gate-effect", codes)

    def test_macro_regime_gate_reduces_inputs_deterministically(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_regime_gate"] = {
            "aggregate_status": "supportive",
            "decision_effect": "pass",
            "inputs": [
                {
                    "scope": "sector",
                    "key": "情報・通信業",
                    "status": "supportive",
                    "source_ref": (
                        "records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml"
                    ),
                },
                {
                    "scope": "event",
                    "key": "earnings",
                    "status": "adverse",
                    "source_ref": (
                        "records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml"
                    ),
                },
            ],
        }
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.macro-aggregate-status", codes)
        self.assertIn("research.macro-decision-effect", codes)

    def test_sector_macro_input_must_match_research_sector(self) -> None:
        front = _minimal_research_front_matter()
        front["sector_33"] = "機械"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.macro-sector-join", codes)

    def test_independent_evidence_count_uses_effective_decisions(self) -> None:
        front = _minimal_research_front_matter()
        front["independent_evidence_count"] = 2
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.independent-evidence-count", codes)

    def test_conviction_tier_is_recomputed_from_policy_rules(self) -> None:
        front = _minimal_research_front_matter()
        front["conviction_tier"] = "high"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.conviction-tier-derived", codes)

    def test_sizing_is_recomputed_from_policy_and_exposure_snapshots(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["real_order_intent_yen"] = 100000
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.real-order-intent-yen", codes)

    def test_single_evidence_real_order_intent_uses_policy_cap(self) -> None:
        front = _minimal_research_front_matter()
        front["conviction_tier"] = "high"
        front["conviction_tier_path"] = "depth"
        front["depth_verification_ref"] = "records/_external/depth-check.md"
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["paper_proxy_position_size_yen"] = 1500000
        sizing["real_order_intent_yen"] = 100000
        sizing["adv_participation_pct"] = 0.75

        codes = {finding.code for finding in self._findings_for(front)}

        self.assertIn("research.real-order-intent-yen", codes)

    def test_single_evidence_approval_requires_complete_payoff(self) -> None:
        front = _minimal_research_front_matter()
        front["thesis_payoff"] = {}

        codes = {finding.code for finding in self._findings_for(front)}

        self.assertIn("research.single-evidence-payoff-confirmation", codes)

    def test_adv_participation_is_recomputed_from_paper_proxy_and_adv(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["adv_participation_pct"] = 99.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.adv-participation-pct", codes)

    def test_position_sizing_overlay_rejects_deprecated_fields(self) -> None:
        front = _minimal_research_front_matter()
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["liquidity_cap_participation_pct"] = 0.5
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.position-sizing-deprecated-field", codes)

    def test_position_sizing_overlay_requires_mapping(self) -> None:
        front = _minimal_research_front_matter()
        front["position_sizing_overlay"] = []
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.type", codes)
        self.assertIn("research.position-sizing-shape", codes)

    def test_position_sizing_overlay_requires_canonical_fields(self) -> None:
        front = _minimal_research_front_matter()
        front["position_sizing_overlay"] = {}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.required", codes)
        self.assertIn("research.position-sizing-missing-field", codes)

    def test_nested_valuation_liquidity_participation_is_deprecated(self) -> None:
        front = _minimal_research_front_matter()
        front["valuation"] = {"liquidity_cap_participation_pct": 0.5}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.valuation-deprecated-field", codes)

    def test_non_approved_sizing_fields_must_be_zero(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "deferred", "posture": "wait_for_event"}
        sizing = front["position_sizing_overlay"]
        assert isinstance(sizing, dict)
        sizing["real_order_intent_yen"] = None
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.rejected-sizing", codes)

    def test_independent_evidence_count_uses_candidate_components(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidates_path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            candidates_path.parent.mkdir(parents=True)
            candidates_path.write_text(
                yaml.safe_dump(
                    {
                        "candidates": [
                            {
                                "ticker": "2767",
                                "evidence_hits": [
                                    {
                                        "evidence_hit_id": "eh-1",
                                        "independence_component_id": "shared-component",
                                    },
                                    {
                                        "evidence_hit_id": "eh-2",
                                        "independence_component_id": "unselected-component",
                                    },
                                ],
                            }
                        ]
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            front = _minimal_research_front_matter()
            front["candidate_evidence_decisions"] = [
                {
                    "evidence_hit_id": "eh-1",
                    "effective_sizing_eligible": True,
                    "evaluated_at": "2026-05-05T20:00:00+09:00",
                    "reason_code": "source_status_ok",
                },
                {
                    "evidence_hit_id": "eh-2",
                    "effective_sizing_eligible": True,
                    "evaluated_at": "2026-05-05T20:00:00+09:00",
                    "reason_code": "source_status_ok",
                },
            ]
            front["selected_supporting_evidence_refs"] = [
                {"source": "candidate", "evidence_hit_id": "eh-1"}
            ]
            front["independent_evidence_count"] = 2
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.independent-evidence-count", codes)

    def test_corporate_action_invalidation_uses_metric_catalog_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog_path = root / "records/_config/metric-catalog/2026-05-01T000000+0900.yaml"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(
                yaml.safe_dump(
                    {
                        "metrics": [
                            {
                                "metric_id": "p_s",
                                "event_invalidation_rules": [{"corporate_action_kind": "merger"}],
                            }
                        ]
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            candidates_path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            candidates_path.parent.mkdir(parents=True)
            candidates_path.write_text(
                yaml.safe_dump(
                    {
                        "run_id": "screening-20260501",
                        "candidates": [
                            {
                                "ticker": "2767",
                                "candidate_id": "candidate-2026-05-01-2767",
                                "screen_run_id": "screening-20260501",
                                "evidence_hits": [
                                    {
                                        "evidence_hit_id": "eh-1",
                                        "source_metric_ids": ["p_s"],
                                        "independence_component_id": "component-1",
                                    }
                                ],
                            }
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            calendar_path = root / "records/_calendars/corporate-actions/2026-05.yaml"
            calendar_path.parent.mkdir(parents=True)
            calendar_path.write_text(
                yaml.safe_dump(
                    {
                        "covered_from": "2026-05-01",
                        "covered_until": "2026-05-31",
                        "last_refreshed_at": "2026-05-05T00:00:00+09:00",
                        "source_status": "ok",
                        "events": [
                            {
                                "ticker": "2767",
                                "corporate_action_kind": "split",
                                "invalidates_metrics": ["p_s"],
                            }
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            front = _minimal_research_front_matter()
            front["candidate_evidence_decisions"] = [
                {
                    "evidence_hit_id": "eh-1",
                    "effective_sizing_eligible": False,
                    "evaluated_at": "2026-05-05T20:00:00+09:00",
                    "reason_code": "corporate_action_post_snapshot",
                    "corporate_action_kind": "split",
                    "invalidated_metric_ids": ["p_s"],
                }
            ]
            front["research_decision"] = {
                "outcome": "rejected",
                "posture": "dropped",
                "rejection_reason": "corporate_action_post_snapshot",
            }
            front["independent_evidence_count"] = 0
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.corporate-action-catalog-mismatch", codes)

    def test_repository_research_requires_existing_candidate_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            front = _minimal_research_front_matter()
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-ref-missing", codes)

    def test_repository_research_candidate_ref_screen_run_id_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            candidate_ref = front["candidate_ref"]
            assert isinstance(candidate_ref, dict)
            candidate_ref["screen_run_id"] = "screening-20260508"
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-ref-screen-run-id", codes)

    def test_repository_research_candidate_ref_candidate_id_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            candidate_ref = front["candidate_ref"]
            assert isinstance(candidate_ref, dict)
            candidate_ref["candidate_id"] = "candidate-2026-05-01-9999"
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-ref-match", codes)

    def test_repository_research_candidate_ref_requires_screen_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            candidate_ref = front["candidate_ref"]
            assert isinstance(candidate_ref, dict)
            del candidate_ref["screen_run_id"]
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-ref-screen-run-id", codes)

    def test_repository_research_lineage_runs_without_evidence_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            del front["candidate_evidence_decisions"]
            candidate_ref = front["candidate_ref"]
            assert isinstance(candidate_ref, dict)
            candidate_ref["screen_run_id"] = "screening-20260508"
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.required", codes)
        self.assertIn("research.candidate-ref-screen-run-id", codes)

    def test_repository_research_selected_evidence_must_exist_in_candidate_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            front["selected_supporting_evidence_refs"] = [
                {"source": "candidate", "evidence_hit_id": "missing-evidence"}
            ]
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.selected-evidence-missing", codes)

    def test_repository_research_selected_candidate_evidence_must_be_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            decisions = front["candidate_evidence_decisions"]
            assert isinstance(decisions, list)
            decision = decisions[0]
            assert isinstance(decision, dict)
            decision["effective_sizing_eligible"] = False
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.selected-evidence-not-eligible", codes)

    def test_repository_research_copied_candidate_fields_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            front["avg_turnover_oku"] = 99.0
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-field-copy", codes)

    def test_repository_research_candidate_metric_copy_cannot_be_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            front = _minimal_research_front_matter()
            front["avg_turnover_oku"] = 2.0
            front["market_cap_oku"] = 100
            front["valuation"] = {}
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-field-copy", codes)

    def test_repository_research_candidate_null_metric_cannot_be_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            candidates_path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            document = yaml.safe_load(candidates_path.read_text(encoding="utf-8"))
            document["candidates"][0]["metrics"]["p_s"] = None
            candidates_path.write_text(
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            front = _minimal_research_front_matter()
            front["avg_turnover_oku"] = 2.0
            front["market_cap_oku"] = 100
            front["valuation"] = {"p_s": 0.5}
            research_path = root / "records/05-research/2026/05/2026-05-05-2767.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {
                finding.code
                for finding in validate_research_file(
                    research_path, playbooks_root=ROOT / "records/_playbooks"
                )
            }

        self.assertIn("research.candidate-field-copy", codes)

    def test_approved_requires_selected_supporting_evidence(self) -> None:
        front = _minimal_research_front_matter()
        front["selected_supporting_evidence_refs"] = []
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.supporting-evidence-required", codes)

    def test_approved_requires_risk_or_contradicting_evidence_review(self) -> None:
        front = _minimal_research_front_matter()
        front["research_evidence_hits"] = [
            {
                "evidence_hit_id": "support-1",
                "decision_role": "sizing_evidence",
                "evidence_polarity": "supports",
            }
        ]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.risk-evidence-required", codes)

    def test_approved_risk_evidence_must_be_separate_from_sizing_evidence(self) -> None:
        front = _minimal_research_front_matter()
        front["research_evidence_hits"] = [
            {
                "evidence_hit_id": "risk-1",
                "decision_role": "sizing_evidence",
                "evidence_polarity": "risk",
            }
        ]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.risk-evidence-required", codes)

    def test_payoff_order_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["stop_loss_yen"] = 1050
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.payoff-order", codes)

    def test_expected_upside_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_upside_pct"] = 20.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.expected-upside", codes)

    def test_expected_downside_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_downside_pct"] = 8.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.expected-downside", codes)

    def test_risk_reward_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["risk_reward_ratio"] = 1.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.risk-reward", codes)

    def test_backdated_analyst_approval_rule_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            approval_root = root / "records/_approval-rules"
            approval_root.mkdir(parents=True)
            approval_file = approval_root / "2026-05-06T000000+0900.yaml"
            approval_file.write_text(
                yaml.safe_dump(
                    {
                        "registry_id": "approval-rules-test",
                        "effective_from": "2026-05-01T00:00:00+09:00",
                        "rules": [
                            {
                                "approval_rule_id": "late-rule",
                                "created_at": "2026-05-06T00:00:00+09:00",
                                "creation_motive": "prospective_policy",
                                "target_record_refs": [],
                                "max_valid_days": 30,
                            }
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (approval_root / "_changelog.jsonl").write_text(
                (
                    '{"event_at":"2026-05-01T00:00:00+09:00",'
                    '"snapshot_path":"records/_approval-rules/2026-05-06T000000+0900.yaml"}\n'
                ),
                encoding="utf-8",
            )
            front = _minimal_research_front_matter()
            front["recorded_at"] = "2026-05-05T20:00:00+09:00"
            front["research_evidence_hits"].append(
                {
                    "evidence_hit_id": "analyst-1",
                    "decision_role": "sizing_evidence",
                    "evidence_polarity": "supports",
                    "analyst_asserted": True,
                    "sizing_eligible": True,
                    "approval_rule_id": "late-rule",
                    "approved_at": "2026-05-05T20:00:00+09:00",
                    "source_refs": [
                        {
                            "ref_path": "records/_external/test.md",
                            "content_sha256": _DIGEST,
                        }
                    ],
                }
            )
            research_path = root / "records/05-research/2026/05/test.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text(
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n"
                + _DEFAULT_BODY,
                encoding="utf-8",
            )

            codes = {finding.code for finding in validate_research_file(research_path)}

        self.assertIn("research.approval-rule-active", codes)

    def test_snapshot_hash_must_be_full_sha256(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook_snapshot"] = _snapshot("records/_playbooks/valuation-reversion/x.md", "bad")
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.snapshot-hash", codes)

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["ticker"] = "abc"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.ticker-format", codes)

    def test_unknown_playbook_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook_id"] = "unknown-playbook"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.unknown-playbook", codes)

    def test_missing_required_section_is_flagged(self) -> None:
        body = "# Research\n\n## 1. Thesis\nonly thesis\n"
        codes = {
            finding.code for finding in self._findings_for(_minimal_research_front_matter(), body)
        }
        self.assertIn("research.missing-section", codes)

    def test_no_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("# Research\n\nno front matter\n")
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "research.no-front-matter")

    def test_front_matter_non_mapping_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\n- a\n- b\n---\n# body\n")
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "research.front-matter-non-mapping")

    def test_missing_playbook_schema_at_load_time_is_handled(self) -> None:
        front = _minimal_research_front_matter()
        path = self._write(front)
        try:
            with (
                tempfile.TemporaryDirectory() as empty_root,
                patch(
                    "baibai_loop.validate.research.discover_playbook_schemas",
                    return_value={"valuation-reversion"},
                ),
            ):
                findings = validate_research_file(path, playbooks_root=Path(empty_root))
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("research.playbook-schema", codes)

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write('---\nticker: "2767\n---\n# body\n')
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertTrue(
            codes & {"research.invalid-yaml", "research.no-front-matter"},
            f"expected parser failure code, got {codes}",
        )

    def test_repository_research_files_pass(self) -> None:
        repo_research = ROOT / "records/05-research"
        files = discover_research_files(repo_research)
        if not files:
            self.skipTest("no research files under repository root")
        for path in files:
            findings = [
                finding
                for finding in validate_research_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
                if finding.severity == "error"
            ]
            self.assertEqual(findings, [], f"research {path} produced error findings: {findings}")

    def test_external_refs_with_external_prefix_requires_object_ref(self) -> None:
        front = _minimal_research_front_matter()
        front["external_refs"] = ["records/_external/chatgpt-5/2026-05-04-9682.md"]
        codes = {
            finding.code for finding in self._findings_for(front) if finding.severity == "error"
        }
        self.assertIn("external-ref.shape", codes)

    def test_sector_concentration_warns_for_three_approved_memos(self) -> None:
        base = _minimal_research_front_matter()
        paths_with_front = [
            (Path(f"research-{index}.md"), {**base, "ticker": f"13{index}A"}) for index in range(3)
        ]
        findings = validate_research_collection(paths_with_front)
        self.assertEqual(len(findings), 3)
        self.assertEqual({finding.code for finding in findings}, {"research.sector-concentration"})

    def test_sector_concentration_ignores_two_approved_memos(self) -> None:
        base = _minimal_research_front_matter()
        findings = validate_research_collection(
            [
                (Path("research-1.md"), {**base, "ticker": "130A"}),
                (Path("research-2.md"), {**base, "ticker": "131A"}),
            ]
        )
        self.assertEqual(findings, [])

    def test_sector_concentration_ignores_rejected_memos(self) -> None:
        base = _minimal_research_front_matter()
        rejected = {
            **base,
            "ticker": "132A",
            "research_decision": {
                "outcome": "rejected",
                "posture": "dropped",
                "rejection_reason": "thesis_failed",
            },
        }
        findings = validate_research_collection(
            [
                (Path("research-1.md"), {**base, "ticker": "130A"}),
                (Path("research-2.md"), {**base, "ticker": "131A"}),
                (Path("research-3.md"), rejected),
            ]
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
