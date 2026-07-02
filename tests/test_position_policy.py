from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.position.policy import PORTFOLIO_POLICY, validate_policy


class PolicyConfigTests(unittest.TestCase):
    def test_portfolio_policy_config_is_complete(self) -> None:
        validate_policy()

    def test_portfolio_policy_config_fails_fast_on_missing_threshold(self) -> None:
        policy = copy.deepcopy(PORTFOLIO_POLICY)
        del policy["risk_budget"]["max_ticker_real_concentration_pct"]

        with self.assertRaisesRegex(RuntimeError, "max_ticker_real_concentration_pct"):
            validate_policy(policy)


if __name__ == "__main__":
    unittest.main()
