from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.policy import discover_policy_files, validate_policy_file


class PolicyValidationTests(unittest.TestCase):
    def test_discovers_policy_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "docs/portfolio-policy.md"
            policy.parent.mkdir(parents=True)
            policy.write_text(_valid_policy_doc(), encoding="utf-8")

            self.assertEqual(discover_policy_files(root), [policy])

    def test_accepts_repository_policy_doc(self) -> None:
        findings = validate_policy_file(ROOT / "docs/portfolio-policy.md")

        self.assertEqual(findings, [])

    def test_rejects_missing_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text("# Different Doc\n\nSee position/policy.py.\n", encoding="utf-8")

            findings = validate_policy_file(path)

        self.assertIn("policy.heading", {finding.code for finding in findings})

    def test_warns_when_code_config_reference_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text("# Portfolio Policy\n\nHuman-readable policy.\n", encoding="utf-8")

            findings = validate_policy_file(path)

        self.assertIn("policy.code-config-note", {finding.code for finding in findings})

    def test_accepts_minimal_policy_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.md"
            path.write_text(_valid_policy_doc(), encoding="utf-8")

            findings = validate_policy_file(path)

        self.assertEqual(findings, [])


def _valid_policy_doc() -> str:
    return "# Portfolio Policy\n\nConcrete thresholds live in `position/policy.py`.\n"


if __name__ == "__main__":
    unittest.main()
