from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.references import validate_reference_integrity


class ReferenceIntegrityValidationTests(unittest.TestCase):
    def test_rejects_invalid_markdown_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text("---\nplaybook_ref: [\n---\n# broken\n", encoding="utf-8")

            findings = validate_reference_integrity(root)

        self.assertIn("reference.invalid-yaml", {finding.code for finding in findings})

    def test_rejects_missing_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "playbook_ref:\n"
                "  ref_path: records/_playbooks/missing/2026-05-01T000000+0900.md\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-not-found", {finding.code for finding in findings})

    def test_rejects_non_mapping_known_reference_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text("---\nplaybook_ref: /tmp/playbook.md\n---\n", encoding="utf-8")

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_known_reference_mapping_without_ref_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\nplaybook_ref:\n  playbook_id: test\n---\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_list_reference_item_without_ref_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scan = root / "records/07-reviews/references.yaml"
            scan.parent.mkdir(parents=True)
            scan.write_text(
                "source_decision_register_refs:\n- decision_event_id: decision-1\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_non_mapping_list_reference_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scan = root / "records/07-reviews/references.yaml"
            scan.parent.mkdir(parents=True)
            scan.write_text("source_trade_refs:\n- records/06-trades/x.md\n", encoding="utf-8")

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-shape", {finding.code for finding in findings})

    def test_rejects_list_reference_item_wrong_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/05-research/2026/05/research.md"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("---\nticker: '1111'\n---\n", encoding="utf-8")
            scan = root / "records/07-reviews/references.yaml"
            scan.parent.mkdir(parents=True)
            scan.write_text(
                "source_trade_refs:\n- ref_path: records/05-research/2026/05/research.md\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

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

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_absolute_scalar_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True, exist_ok=True)
            selection.write_text("candidates_ref: /tmp/candidates.yaml\n", encoding="utf-8")

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})

    def test_accepts_relative_scalar_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "records/04-candidates/2026/05/candidates.yaml"
            candidates.parent.mkdir(parents=True)
            candidates.write_text("candidates: []\n", encoding="utf-8")
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True, exist_ok=True)
            selection.write_text(
                "candidates_ref: records/04-candidates/2026/05/candidates.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertEqual(findings, [])

    def test_rejects_scalar_candidates_ref_to_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = root / "records/04-candidates/2026/05/candidates.yaml"
            candidates.parent.mkdir(parents=True)
            candidates.write_text("- not-a-mapping\n", encoding="utf-8")
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True, exist_ok=True)
            selection.write_text(
                "candidates_ref: records/04-candidates/2026/05/candidates.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_scalar_candidates_ref_to_wrong_mapping_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/04-candidates/2026/05/runs.yaml"
            runs.parent.mkdir(parents=True)
            runs.write_text("runs: []\n", encoding="utf-8")
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True, exist_ok=True)
            selection.write_text(
                "candidates_ref: records/04-candidates/2026/05/runs.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_scalar_macro_context_ref_to_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            macro = root / "records/01-macro-context/2026/05/macro-context.yaml"
            macro.parent.mkdir(parents=True)
            macro.write_text("- not-a-mapping\n", encoding="utf-8")
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True, exist_ok=True)
            selection.write_text(
                "macro_context_ref: records/01-macro-context/2026/05/macro-context.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-parse", {finding.code for finding in findings})

    def test_rejects_missing_macro_context_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True)
            selection.write_text(
                "macro_context_ref: records/01-macro-context/2026/05/missing.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-not-found", {finding.code for finding in findings})

    def test_rejects_scalar_research_ref_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = root / "records/_ledger/research-decisions/2026-05.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"research_ref":"/tmp/research.md"}\n', encoding="utf-8")

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})

    def test_rejects_input_ref_wrong_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/01-macro-context/2026/05/macro-context.yaml"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("kind: macro-context\n", encoding="utf-8")
            manifest = root / "records/04-candidates/2026/05/manifest.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                "input_refs:\n"
                "  screening_rules:\n"
                "    ref_path: records/01-macro-context/2026/05/macro-context.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-prefix", {finding.code for finding in findings})

    def test_rejects_macro_context_ref_missing_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            macro = root / "records/01-macro-context/2026/05/macro-context.yaml"
            macro.parent.mkdir(parents=True)
            macro.write_text("kind: macro-context\n", encoding="utf-8")
            selection = root / "records/04-candidates/2026/05/selection.yaml"
            selection.parent.mkdir(parents=True)
            selection.write_text(
                "macro_context_ref: records/01-macro-context/2026/05/macro-context.yaml\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

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

            findings = validate_reference_integrity(root)

        self.assertIn("reference.universe-members", {finding.code for finding in findings})

    def test_accepts_valid_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            playbook = root / "records/_playbooks/test/2026-05-01T000000+0900.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("---\nplaybook_id: test\n---\n# Playbook\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "playbook_ref:\n"
                "  ref_path: records/_playbooks/test/2026-05-01T000000+0900.md\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertEqual(findings, [])

    def test_rejects_path_traversal_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\nplaybook_ref:\n  ref_path: ../outside.md\n---\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})

    def test_rejects_wrong_suffix_repository_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/_playbooks/test/policy.txt"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("[project]\nname = 'x'\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\nplaybook_ref:\n  ref_path: records/_playbooks/test/policy.txt\n---\n",
                encoding="utf-8",
            )

            findings = validate_reference_integrity(root)

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

            findings = validate_reference_integrity(root)

        self.assertIn("reference.ref-front-matter", {finding.code for finding in findings})

    def test_rejects_symlink_reference_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root.parent / f"{root.name}-outside.yaml"
            outside.write_text("policy_id: outside\n", encoding="utf-8")
            policy_link = root / "records/_playbooks/test/2026-05-01T000000+0900.md"
            policy_link.parent.mkdir(parents=True)
            try:
                policy_link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is not supported")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "playbook_ref:\n"
                "  ref_path: records/_playbooks/test/2026-05-01T000000+0900.md\n"
                "---\n",
                encoding="utf-8",
            )

            try:
                findings = validate_reference_integrity(root)
            finally:
                outside.unlink(missing_ok=True)

        self.assertIn("reference.ref-path", {finding.code for finding in findings})


if __name__ == "__main__":
    unittest.main()
