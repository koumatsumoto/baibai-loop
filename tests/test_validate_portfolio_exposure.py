from __future__ import annotations

import hashlib
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
                    extra_order="- origin_order_intent_id: intent-1\n  guarded_notional_yen: 0\n"
                ),
                encoding="utf-8",
            )

            findings = validate_portfolio_exposure_file(path)

        self.assertIn("portfolio-exposure.duplicate-order", {finding.code for finding in findings})

    def test_rebuild_fails_when_source_trade_order_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            trade = root / "records/06-trades/2026/05/2026-05-05-9682.md"
            trade.parent.mkdir(parents=True)
            trade.write_text(_trade(), encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(trade.read_bytes()).hexdigest()
            snapshot = root / "records/_portfolio-exposure/2026/05/exposure.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                _snapshot(
                    source_trade_ref=(
                        "source_trade_refs:\n"
                        "- ref_path: records/06-trades/2026/05/2026-05-05-9682.md\n"
                        f"  content_sha256: {digest}\n"
                    ),
                    orders="outstanding_orders: []\n",
                    remaining=1000000,
                ),
                encoding="utf-8",
            )

            findings = validate_portfolio_exposure_file(snapshot)

        self.assertIn("portfolio-exposure.rebuild", {finding.code for finding in findings})

    def test_rebuild_requires_source_trade_refs_when_trade_has_outstanding_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            trade = root / "records/06-trades/2026/05/2026-05-05-9682.md"
            trade.parent.mkdir(parents=True)
            trade.write_text(_trade(), encoding="utf-8")
            snapshot = root / "records/_portfolio-exposure/2026/05/exposure.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                _snapshot(source_trade_ref="source_trade_refs: []\n"),
                encoding="utf-8",
            )

            findings = validate_portfolio_exposure_file(snapshot)

        self.assertIn(
            "portfolio-exposure.source-trades-required", {finding.code for finding in findings}
        )


def _snapshot(
    *,
    extra_order: str = "",
    source_trade_ref: str = "",
    orders: str | None = None,
    remaining: int = 790000,
) -> str:
    orders_block = (
        orders
        if orders is not None
        else (
            "outstanding_orders:\n"
            "- origin_order_intent_id: intent-1\n"
            "  guarded_notional_yen: 210000\n"
            f"{extra_order}"
        )
    )
    return (
        "snapshot_id: exposure-1\n"
        'as_of: "2026-05-05T20:00:00+09:00"\n'
        "positions: []\n"
        "exposures: []\n"
        f"{source_trade_ref}"
        f"{orders_block}"
        "tactical_real_budget_yen: 1000000\n"
        f"remaining_tactical_real_budget_yen: {remaining}\n"
    )


def _trade() -> str:
    return textwrap.dedent(
        """\
        ---
        trade_id: trade-1
        ticker: '9682'
        playbook_id: sales-discount-growth
        orders:
        - order_id: order-1
          origin_order_intent_id: intent-1
          side: buy
          state: submitted
          submitted_quantity: 200
          filled_quantity: 0
          order_price_guard_yen: 1050
          events:
          - event_id: order-1-submit
            event_type: submit
            at: '2026-05-05T20:00:00+09:00'
        ---
        # Trade
        """
    )


if __name__ == "__main__":
    unittest.main()
