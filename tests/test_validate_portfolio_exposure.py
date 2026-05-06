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

from baibai_loop.validate.portfolio_exposure import (
    discover_portfolio_exposure_files,
    validate_portfolio_exposure_file,
)


class PortfolioExposureValidationTests(unittest.TestCase):
    def test_discovers_portfolio_exposure_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = root / "2026/05/exposure.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(_snapshot(), encoding="utf-8")

            self.assertEqual(discover_portfolio_exposure_files(root), [snapshot])

    def test_accepts_repository_snapshots(self) -> None:
        root = ROOT / "records/_portfolio-exposure"
        findings = [
            finding
            for path in discover_portfolio_exposure_files(root)
            for finding in validate_portfolio_exposure_file(path)
        ]

        self.assertEqual(findings, [])

    def test_rejects_remaining_budget_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exposure.yaml"
            path.write_text(
                _snapshot().replace(
                    "remaining_tactical_real_budget_yen: 790000",
                    "remaining_tactical_real_budget_yen: 800000",
                ),
                encoding="utf-8",
            )

            findings = validate_portfolio_exposure_file(path)

        self.assertIn("portfolio-exposure.remaining-budget", {finding.code for finding in findings})

    def test_rejects_duplicate_order_intent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exposure.yaml"
            path.write_text(
                _snapshot(
                    extra_order="        - origin_order_intent_id: intent-1\n"
                    "          guarded_notional_yen: 0\n"
                ),
                encoding="utf-8",
            )

            findings = validate_portfolio_exposure_file(path)

        self.assertIn("portfolio-exposure.duplicate-order", {finding.code for finding in findings})


def _snapshot(*, extra_order: str = "") -> str:
    return textwrap.dedent(
        f"""\
        snapshot_id: exposure-1
        as_of: "2026-05-05T20:00:00+09:00"
        positions: []
        exposures: []
        outstanding_orders:
        - origin_order_intent_id: intent-1
          guarded_notional_yen: 210000
{extra_order}\
        tactical_real_budget_yen: 1000000
        remaining_tactical_real_budget_yen: 790000
        """
    )


if __name__ == "__main__":
    unittest.main()
