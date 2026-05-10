from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.snapshots import validate_snapshot_integrity


class SnapshotIntegrityValidationTests(unittest.TestCase):
    def test_rejects_removed_hash_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            hash_key = "content_" + "sha256"
            research.write_text(
                "---\n"
                "policy_ref:\n"
                "  ref_path: records/01-policy/2026/05/policy.yaml\n"
                f"  {hash_key}: sha256:{'0' * 64}\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.removed-hash-field", {finding.code for finding in findings})

    def test_rejects_missing_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: records/01-policy/2026/05/policy.yaml\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-not-found", {finding.code for finding in findings})

    def test_accepts_valid_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text("policy_id: portfolio-policy\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: records/01-policy/2026/05/policy.yaml\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertEqual(findings, [])

    def test_rejects_path_traversal_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: ../outside.yaml\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})


if __name__ == "__main__":
    unittest.main()
