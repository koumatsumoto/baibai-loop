from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.policy import discover_policy_files, validate_policy_file


class PolicyValidationTests(unittest.TestCase):
    def test_discovers_policy_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "2026/05/2026-05-01T000000+0900-portfolio-policy.md"
            policy.parent.mkdir(parents=True)
            policy.write_text(_valid_policy_front_matter(), encoding="utf-8")

            self.assertEqual(discover_policy_files(root), [policy])

    def test_accepts_repository_policy_snapshot(self) -> None:
        findings = validate_policy_file(
            ROOT / "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
        )

        self.assertEqual(findings, [])

    def test_rejects_tier_cap_above_policy_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text(
                _valid_policy_front_matter().replace(
                    "max_real_order_notional_yen: 500000",
                    "max_real_order_notional_yen: 200000",
                    1,
                ),
                encoding="utf-8",
            )

            findings = validate_policy_file(path)

        self.assertIn("policy.tier-real-cap", {finding.code for finding in findings})

    def test_rejects_missing_minimum_payoff_override_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text(
                _valid_policy_front_matter().replace(
                    "\n  minimum_payoff.min_expected_upside_pct:\n"
                    "    override_allowed: true\n"
                    "    override_severity: warning\n"
                    "    requires_rationale_ref: true\n"
                    "    requires_review_flag: true\n",
                    "\n",
                ),
                encoding="utf-8",
            )

            findings = validate_policy_file(path)

        self.assertIn("policy.minimum-payoff-rule", {finding.code for finding in findings})

    def test_rejects_non_monotonic_conviction_count_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text(
                _valid_policy_front_matter().replace(
                    "high_min_independent_evidence_count: 3",
                    "high_min_independent_evidence_count: 0",
                ),
                encoding="utf-8",
            )

            findings = validate_policy_file(path)

        self.assertIn("policy.conviction-count-monotonicity", {finding.code for finding in findings})

    def test_rejects_weak_minimum_payoff_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text(
                _valid_policy_front_matter().replace(
                    "min_risk_reward_ratio: 1.1",
                    "min_risk_reward_ratio: 1.0",
                    1,
                ),
                encoding="utf-8",
            )

            findings = validate_policy_file(path)

        self.assertIn("policy.minimum-payoff-risk-reward", {finding.code for finding in findings})


def _valid_policy_front_matter() -> str:
    return textwrap.dedent(
        """\
        ---
        policy_id: portfolio-policy
        effective_from: "2026-05-01T00:00:00+09:00"
        policy_origin: codified_post_hoc
        capital_basis:
          real_capital_yen: 5000000
          tactical_real_budget_yen: 1000000
          paper_proxy_capital_yen: 100000000
        risk_budget:
          max_paper_proxy_position_size_yen: 2000000
          max_real_order_notional_yen: 500000
        minimum_payoff:
          min_risk_reward_ratio: 1.1
          min_expected_upside_pct: 10.0
        execution_scaling:
          paper_to_real_order_notional_pct: 21.0
        conviction_tier_caps:
          low:
            max_paper_proxy_position_size_yen: 500000
            max_real_order_notional_yen: 100000
          high:
            max_paper_proxy_position_size_yen: 2000000
            max_real_order_notional_yen: 500000
        conviction_tier_rules:
          count_breadth:
            medium_min_independent_evidence_count: 1
            high_min_independent_evidence_count: 3
          high_depth:
            min_independent_evidence_count: 1
            min_risk_reward_ratio: 2.0
        policy_rules:
          __default__:
            override_allowed: false
            override_severity: error
          minimum_payoff.min_risk_reward_ratio:
            override_allowed: true
            override_severity: warning
            requires_rationale_ref: true
            requires_review_flag: true
          minimum_payoff.min_expected_upside_pct:
            override_allowed: true
            override_severity: warning
            requires_rationale_ref: true
            requires_review_flag: true
        ---
        """
    )


if __name__ == "__main__":
    unittest.main()
