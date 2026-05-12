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

    def test_rejects_removed_hash_field_in_support_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            hash_key = "row_" + "sha256"
            policy.write_text(f"policy_id: test\n{hash_key}: sha256:bad\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.removed-hash-field", {finding.code for finding in findings})

    def test_rejects_removed_reference_field_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.md"
            policy.parent.mkdir(parents=True)
            policy.write_text("---\npolicy_id: portfolio-policy\n---\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_snapshot:\n  ref_path: records/01-policy/2026/05/policy.md\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn(
            "reference.removed-reference-field",
            {finding.code for finding in findings},
        )

    def test_rejects_removed_snapshot_path_fallback_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.md"
            policy.parent.mkdir(parents=True)
            policy.write_text("---\npolicy_id: portfolio-policy\n---\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "policy_ref:\n"
                "  ref_path: records/01-policy/2026/05/policy.md\n"
                "  snapshot_path: records/01-policy/2026/05/policy.md\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn(
            "reference.removed-reference-field",
            {finding.code for finding in findings},
        )

    def test_rejects_invalid_markdown_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text("---\npolicy_ref: [\n---\n# broken\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.invalid-yaml", {finding.code for finding in findings})

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

    def test_rejects_non_mapping_known_reference_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text("---\npolicy_ref: /tmp/policy.md\n---\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_known_reference_mapping_without_ref_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  effective_from: '2026-05-01'\n---\n", encoding="utf-8"
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_non_mapping_calendar_refs_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text("---\ncalendar_refs: []\n---\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_list_reference_item_without_ref_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exposure = root / "records/_portfolio-exposure/2026/05/exposure.yaml"
            exposure.parent.mkdir(parents=True)
            exposure.write_text(
                "source_decision_register_refs:\n- decision_event_id: decision-1\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_non_mapping_nested_calendar_reference_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\ncalendar_refs:\n  business_days: []\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_non_mapping_list_reference_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exposure = root / "records/_portfolio-exposure/2026/05/exposure.yaml"
            exposure.parent.mkdir(parents=True)
            exposure.write_text("source_trade_refs:\n- records/06-trades/x.md\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_list_reference_item_wrong_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/05-research/2026/05/research.md"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("---\nticker: '1111'\n---\n", encoding="utf-8")
            exposure = root / "records/_portfolio-exposure/2026/05/exposure.yaml"
            exposure.parent.mkdir(parents=True)
            exposure.write_text(
                "source_trade_refs:\n- ref_path: records/05-research/2026/05/research.md\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-prefix", {finding.code for finding in findings})

    def test_rejects_repository_reference_to_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/universe.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text("- not-a-mapping\n", encoding="utf-8")
            candidates = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            candidates.parent.mkdir(parents=True)
            candidates.write_text(
                "universe_ref:\n  ref_path: records/_universe-snapshots/2026/05/universe.yaml\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_absolute_scalar_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            selection = root / "records/_benchmarks/domain-model/e2e/selection.yaml"
            selection.parent.mkdir(parents=True)
            selection.write_text("candidates_ref: /tmp/candidates.yaml\n", encoding="utf-8")

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})

    def test_accepts_relative_scalar_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "records/_benchmarks/domain-model/e2e/candidates.yaml"
            candidates.parent.mkdir(parents=True)
            candidates.write_text("candidates: []\n", encoding="utf-8")
            selection = root / "records/_benchmarks/domain-model/e2e/selection.yaml"
            selection.write_text(
                "candidates_ref: records/_benchmarks/domain-model/e2e/candidates.yaml\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertEqual(findings, [])

    def test_rejects_scalar_candidates_ref_to_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "records/_benchmarks/domain-model/e2e/candidates.yaml"
            candidates.parent.mkdir(parents=True)
            candidates.write_text("- not-a-mapping\n", encoding="utf-8")
            selection = root / "records/_benchmarks/domain-model/e2e/selection.yaml"
            selection.write_text(
                "candidates_ref: records/_benchmarks/domain-model/e2e/candidates.yaml\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_scalar_candidates_ref_to_wrong_mapping_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/_benchmarks/domain-model/e2e/runs.yaml"
            runs.parent.mkdir(parents=True)
            runs.write_text("runs: []\n", encoding="utf-8")
            selection = root / "records/_benchmarks/domain-model/e2e/selection.yaml"
            selection.write_text(
                "candidates_ref: records/_benchmarks/domain-model/e2e/runs.yaml\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_scalar_outlook_ref_to_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outlook = root / "records/03-outlook/2026/05/outlook.yaml"
            outlook.parent.mkdir(parents=True)
            outlook.write_text("- not-a-mapping\n", encoding="utf-8")
            selection = root / "records/_benchmarks/domain-model/e2e/selection.yaml"
            selection.parent.mkdir(parents=True)
            selection.write_text(
                "outlook_ref: records/03-outlook/2026/05/outlook.yaml\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_standalone_universe_duplicate_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/universe.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 2\n"
                "members_scope: full_universe\n"
                "members_recorded: 2\n"
                "members:\n"
                "- ticker: '130A'\n"
                "- ticker: '130A'\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.universe-members", {finding.code for finding in findings})

    def test_accepts_valid_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.md"
            policy.parent.mkdir(parents=True)
            policy.write_text("---\npolicy_id: portfolio-policy\n---\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: records/01-policy/2026/05/policy.md\n---\n",
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

    def test_rejects_wrong_suffix_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/01-policy/2026/05/policy.txt"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("[project]\nname = 'x'\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: records/01-policy/2026/05/policy.txt\n---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-suffix", {finding.code for finding in findings})

    def test_rejects_structured_markdown_reference_without_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            playbook = root / "records/_playbooks/test/2026-05-01T000000+0900.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("# Playbook\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "playbook_ref:\n"
                "  ref_path: records/_playbooks/test/2026-05-01T000000+0900.md\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("reference.ref-front-matter", {finding.code for finding in findings})

    def test_rejects_symlink_reference_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root.parent / f"{root.name}-outside.yaml"
            outside.write_text("policy_id: outside\n", encoding="utf-8")
            policy_link = root / "records/01-policy/2026/05/policy.yaml"
            policy_link.parent.mkdir(parents=True)
            try:
                policy_link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is not supported")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\npolicy_ref:\n  ref_path: records/01-policy/2026/05/policy.yaml\n---\n",
                encoding="utf-8",
            )

            try:
                findings = validate_snapshot_integrity(root)
            finally:
                outside.unlink(missing_ok=True)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})


if __name__ == "__main__":
    unittest.main()
