from __future__ import annotations

import io
import sys
import unittest
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import select_sweep_command
from baibai_loop.validate.ledger import validate_ledger_file
from baibai_loop.validate.research import validate_research_file
from baibai_loop.validate.trade import validate_trade_file


class OperationalE2ETests(unittest.TestCase):
    def test_loose_selection_connects_to_existing_research_trade_and_ledger(self) -> None:
        buffer = io.StringIO()
        exit_code = select_sweep_command(
            asof_date=date(2026, 5, 8),
            macro_context_path=(
                ROOT / "records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml"
            ),
            top=10,
            profiles=("loose",),
            candidates_root=ROOT / "records/04-candidates",
            macro_context_root=ROOT / "records/01-macro-context",
            stdout=buffer,
        )
        self.assertEqual(exit_code, 0)
        payload = yaml.safe_load(buffer.getvalue())
        profile = payload["profiles"][0]
        self.assertIn("8255", profile["recommended_tickers"])

        checked_paths = [
            ROOT / "records/05-research/2026/05/2026-05-13-8255-cashflow-yield-discount.md",
            ROOT / "records/06-trades/2026/05/2026-05-13-8255.md",
            ROOT / "records/_ledger/research-decisions/2026-05.jsonl",
        ]
        findings = [
            *validate_research_file(checked_paths[0]),
            *validate_trade_file(checked_paths[1]),
            *validate_ledger_file(checked_paths[2]),
        ]
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected validation errors: {errors}")


if __name__ == "__main__":
    unittest.main()
