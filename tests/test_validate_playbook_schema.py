from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.playbook_schema import (
    PlaybookSchema,
    PlaybookSchemaError,
    load_playbook_schema,
    validate_research_body,
)


class PlaybookSchemaLoaderTests(unittest.TestCase):
    def test_load_repository_valuation_mean_reversion_schema(self) -> None:
        schema = load_playbook_schema(ROOT / "playbooks", "valuation-mean-reversion-v1")
        self.assertIsInstance(schema, PlaybookSchema)
        self.assertEqual(schema.name, "valuation-mean-reversion-v1")
        self.assertGreaterEqual(len(schema.body_sections), 13)

    def test_missing_schema_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, self.assertRaises(FileNotFoundError):
            load_playbook_schema(Path(tmpdir), "no-such-playbook")

    def test_invalid_schema_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_path = Path(tmpdir) / "broken.schema.yaml"
            schema_path.write_text("- not-a-mapping\n", encoding="utf-8")
            with self.assertRaises(PlaybookSchemaError):
                load_playbook_schema(Path(tmpdir), "broken")

    def test_missing_title_pattern_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_path = Path(tmpdir) / "broken.schema.yaml"
            schema_path.write_text(
                "name: broken\nbody_sections:\n  - required: true\n",
                encoding="utf-8",
            )
            with self.assertRaises(PlaybookSchemaError):
                load_playbook_schema(Path(tmpdir), "broken")

    def test_invalid_regex_pattern_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_path = Path(tmpdir) / "broken.schema.yaml"
            schema_path.write_text(
                "name: broken\nbody_sections:\n  - title_pattern: '[invalid'\n",
                encoding="utf-8",
            )
            with self.assertRaises(PlaybookSchemaError):
                load_playbook_schema(Path(tmpdir), "broken")


class ResearchBodyValidationTests(unittest.TestCase):
    def test_validate_research_body_finds_missing_required_section(self) -> None:
        schema = load_playbook_schema(ROOT / "playbooks", "valuation-mean-reversion-v1")
        body = "## 1. Thesis\ntext\n"
        findings = validate_research_body(Path("dummy.md"), body, schema)
        codes = {f.code for f in findings}
        self.assertIn("research.missing-section", codes)
        # Thesis is present, so at least one section was satisfied
        missing_messages = [f.message for f in findings]
        self.assertFalse(any("Thesis" in msg for msg in missing_messages))

    def test_validate_research_body_skips_optional_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_path = Path(tmpdir) / "demo.schema.yaml"
            schema_path.write_text(
                "name: demo\nbody_sections:\n"
                "  - title_pattern: 'Required'\n"
                "  - title_pattern: 'Optional'\n    required: false\n",
                encoding="utf-8",
            )
            schema = load_playbook_schema(Path(tmpdir), "demo")
            body = "## Required\ntext\n"
            findings = validate_research_body(Path("dummy.md"), body, schema)
            self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
