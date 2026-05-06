from __future__ import annotations

import hashlib
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
    def test_rejects_snapshot_payload_self_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = root / "records/_universe-snapshots/2026/05/universe.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                f"snapshot_id: universe-20260501\ncontent_sha256: sha256:{'0' * 64}\nmembers: []\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("snapshot.self-hash", {finding.code for finding in findings})

    def test_rejects_snapshot_reference_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text("policy_id: portfolio-policy\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "portfolio_policy_snapshot:\n"
                "  ref_path: records/01-policy/2026/05/policy.yaml\n"
                f"  content_sha256: sha256:{'1' * 64}\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("snapshot.hash-mismatch", {finding.code for finding in findings})

    def test_accepts_valid_snapshot_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text("policy_id: portfolio-policy\n", encoding="utf-8")
            digest = hashlib.sha256(policy.read_bytes()).hexdigest()
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "portfolio_policy_snapshot:\n"
                "  ref_path: records/01-policy/2026/05/policy.yaml\n"
                f"  content_sha256: sha256:{digest}\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
