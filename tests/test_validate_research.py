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
        "research_decision": {"outcome": "approved", "posture": "act_now"},
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
        "ai_draft": True,
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
                        "sector_33": "情報・通信業",
                        "avg_turnover_oku": 2.0,
                        "market_cap_oku": 100,
                        "metrics": {"p_s": 0.5},
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "playbook_id": "valuation-reversion",
                                "source_status": "ok",
                                "sizing_eligible": True,
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
                "inputs": {"articles": [], "stats_series": []},
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
            return validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()

    def test_minimal_valid_research_passes(self) -> None:
        findings = self._findings_for(_minimal_research_front_matter())
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_missing_required_field_is_flagged_by_schema(self) -> None:
        front = _minimal_research_front_matter()
        del front["macro_context_fit"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.required", codes)

    def test_unknown_outcome_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "maybe", "posture": "act_now"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.enum", codes)
        self.assertIn("research.unknown-outcome", codes)

    def test_rejected_requires_rejection_reason(self) -> None:
        front = _minimal_research_front_matter()
        front["research_decision"] = {"outcome": "rejected", "posture": "dropped"}
        front["position_sizing_overlay"] = {
            "paper_proxy_position_size_yen": 0,
            "real_order_intent_yen": 0,
            "adv_participation_pct": 0,
        }
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.rejection-reason-required", codes)

    def test_deferred_macro_context_cannot_be_approved(self) -> None:
        front = _minimal_research_front_matter()
        fit = front["macro_context_fit"]
        assert isinstance(fit, dict)
        fit["decision_effect"] = "defer"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.macro-context-defer-approved", codes)

    def test_approved_requires_corporate_action_check(self) -> None:
        front = _minimal_research_front_matter()
        del front["corporate_action_check"]
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.required", codes)
        self.assertIn("research.corporate-action-check", codes)

    def test_corporate_action_check_must_be_checked_for_approved_research(self) -> None:
        front = _minimal_research_front_matter()
        front["corporate_action_check"] = {"checked": False, "result": "none"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.corporate-action-check", codes)

    def test_approved_rejects_found_corporate_action(self) -> None:
        front = _minimal_research_front_matter()
        front["corporate_action_check"] = {"checked": True, "result": "found"}
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.corporate-action-check-result", codes)

    def test_repository_research_requires_existing_candidate_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_macro_context_fixture(root)
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

    def test_repository_research_candidate_ref_matches_by_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            _write_macro_context_fixture(root)
            _write_playbook_ref_fixture(root)
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

            findings = validate_research_file(
                research_path, playbooks_root=ROOT / "records/_playbooks"
            )

        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_repository_research_candidate_ref_ticker_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src").mkdir()
            _write_candidate_fixture(root)
            _write_macro_context_fixture(root)
            _write_playbook_ref_fixture(root)
            front = _minimal_research_front_matter()
            candidate_ref = front["candidate_ref"]
            assert isinstance(candidate_ref, dict)
            candidate_ref["ticker"] = "9999"
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

        self.assertIn("research.candidate-ref-ticker", codes)

    def test_payoff_order_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["stop_loss_yen"] = 700
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.payoff-order", codes)

    def test_expected_upside_formula_is_checked(self) -> None:
        front = _minimal_research_front_matter()
        payoff = front["thesis_payoff"]
        assert isinstance(payoff, dict)
        payoff["expected_upside_pct"] = 99.0
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.expected-upside", codes)

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["ticker"] = "bad"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.ticker-format", codes)

    def test_unknown_playbook_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook_id"] = "unknown-playbook"
        codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.unknown-playbook", codes)

    def test_missing_required_section_is_flagged(self) -> None:
        body = _DEFAULT_BODY.replace("## 6. Catalyst\ntext\n", "")
        codes = {
            finding.code for finding in self._findings_for(_minimal_research_front_matter(), body)
        }
        self.assertIn("research.missing-section", codes)

    def test_no_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("# Research\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_research_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("research.no-front-matter", codes)

    def test_front_matter_non_mapping_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\n- not\n- mapping\n---\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_research_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("research.front-matter-non-mapping", codes)

    def test_missing_playbook_schema_at_load_time_is_handled(self) -> None:
        front = _minimal_research_front_matter()
        with patch(
            "baibai_loop.validate.research.load_playbook_schema",
            side_effect=FileNotFoundError("missing"),
        ):
            codes = {finding.code for finding in self._findings_for(front)}
        self.assertIn("research.playbook-schema", codes)

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\nfoo: [\n---\n")
            path = Path(tmp.name)
        try:
            codes = {
                finding.code
                for finding in validate_research_file(
                    path, playbooks_root=ROOT / "records/_playbooks"
                )
            }
        finally:
            path.unlink()
        self.assertIn("research.invalid-yaml", codes)

    def test_repository_research_files_pass_without_errors(self) -> None:
        repo_research = ROOT / "records/05-research"
        files = discover_research_files(repo_research)
        if not files:
            self.skipTest("no research files under repository root")
        for path in files:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
            errors = [finding for finding in findings if finding.severity == "error"]
            self.assertEqual(errors, [], f"research markdown {path} produced errors: {errors}")

    def test_sector_concentration_warns_for_three_approved_memos(self) -> None:
        fronts = []
        for ticker in ("1111", "2222", "3333"):
            front = _minimal_research_front_matter()
            front["ticker"] = ticker
            fronts.append((Path(f"{ticker}.md"), front))

        findings = validate_research_collection(fronts)

        self.assertEqual(len(findings), 3)
        self.assertTrue(
            all(finding.code == "research.sector-concentration" for finding in findings)
        )


if __name__ == "__main__":
    unittest.main()
