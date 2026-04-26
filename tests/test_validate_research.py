from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.research import (
    discover_research_files,
    validate_research_file,
)

_DEFAULT_BODY = """
# Research

## 1. Thesis
text

## 2. Macro gate
text

## 3. Valuation snapshot
text

## 4. 一時的割安の原因仮説
text

## 5. 反対仮説
text

## 6. Catalyst
text

## 7. Price reaction
text

## 8. Crowding
text

## 9. ミクロ 4 軸寄与度
text

## 10. Entry 条件
text

## 11. Exit 条件
text

## 12. Invalidation
text

## 13. Position size
text
"""


def _minimal_research_front_matter() -> dict[str, object]:
    return {
        "ticker": "2767",
        "name": "Sample Co",
        "playbook": "valuation-mean-reversion-v1",
        "screened_ref": "screened/2026/04/2026-04-24.yaml",
        "view_ref": "view/2026/04/view-2026-04-24-bootstrap.md",
        "brief_refs": [],
        "ai-draft": True,
        "published_at": "2026-04-25T22:00:00+09:00",
        "tradable_at": "2026-05-15T09:00:00+09:00",
        "macro_gate": "neutral",
        "valuation": {"per_trailing": 6.63},
    }


class ResearchValidationTests(unittest.TestCase):
    def _write(self, front_matter: object, body: str = _DEFAULT_BODY) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            front_yaml = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False)
            tmp.write(f"---\n{front_yaml}---\n{body}")
            return Path(tmp.name)

    def test_minimal_valid_research_passes(self) -> None:
        path = self._write(_minimal_research_front_matter())
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_missing_required_field_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["macro_gate"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.missing-field", codes)

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["ticker"] = "abc"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.invalid-ticker", codes)

    def test_unknown_playbook_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook"] = "unknown-playbook"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.unknown-playbook", codes)

    def test_invalid_macro_gate_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_gate"] = "wrong"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.invalid-macro-gate", codes)

    def test_screened_ref_must_be_yaml(self) -> None:
        front = _minimal_research_front_matter()
        front["screened_ref"] = "screened/2026/04/2026-04-24.md"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.screened-ref-not-yaml", codes)

    def test_missing_required_section_is_flagged(self) -> None:
        body = "# Research\n\n## 1. Thesis\nonly thesis\n"
        path = self._write(_minimal_research_front_matter(), body=body)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.missing-section", codes)

    def test_no_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("# Research\n\nno front matter\n")
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "research.no-front-matter")

    def test_front_matter_non_mapping_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\n- a\n- b\n---\n# body\n")
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "research.front-matter-non-mapping")

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write('---\nticker: "2767\n---\n# body\n')
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        # malformed YAML may surface as either invalid-yaml or no-front-matter
        self.assertTrue(
            codes & {"research.invalid-yaml", "research.no-front-matter"},
            f"expected parser failure code, got {codes}",
        )

    def test_repository_research_files_pass(self) -> None:
        repo_research = ROOT / "research"
        files = discover_research_files(repo_research)
        if not files:
            self.skipTest("no research files under repository root")
        for path in files:
            findings = [
                f
                for f in validate_research_file(path, playbooks_root=ROOT / "playbooks")
                if f.severity == "error"
            ]
            self.assertEqual(findings, [], f"research {path} produced error findings: {findings}")


if __name__ == "__main__":
    unittest.main()
