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

from baibai_loop.validate.view import (
    discover_view_files,
    validate_view_file,
)


def _minimal_view_front_matter() -> dict[str, object]:
    return {
        "ai-draft": True,
        "published_at": "2026-04-27T09:00:00+09:00",
        "horizon": "1-6m",
        "sectors": {"機械": "neutral", "電気機器": "tailwind"},
        "regions": {"us": "neutral", "japan-domestic": None},
    }


class ViewValidationTests(unittest.TestCase):
    def _write(self, front_matter: object, body: str = "") -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            front_yaml = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False)
            tmp.write(f"---\n{front_yaml}---\n{body}")
            return Path(tmp.name)

    def test_minimal_valid_view_has_no_findings(self) -> None:
        path = self._write(_minimal_view_front_matter())
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_missing_sectors_is_flagged(self) -> None:
        front = _minimal_view_front_matter()
        del front["sectors"]
        path = self._write(front)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("view.missing-sectors", codes)

    def test_invalid_sector_status_is_error(self) -> None:
        front = _minimal_view_front_matter()
        front["sectors"] = {"機械": "WRONG"}
        path = self._write(front)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("view.invalid-sector-status", codes)

    def test_unknown_sector_is_warning(self) -> None:
        front = _minimal_view_front_matter()
        front["sectors"] = {"機械": "neutral", "未知業種": "tailwind"}
        path = self._write(front)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        warnings = [f for f in findings if f.severity == "warning"]
        self.assertTrue(any(f.code == "view.unknown-sector" for f in warnings))

    def test_unknown_region_is_warning(self) -> None:
        front = _minimal_view_front_matter()
        front["regions"] = {"unknown-region": "neutral"}
        path = self._write(front)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        warnings = [f for f in findings if f.severity == "warning"]
        self.assertTrue(any(f.code == "view.unknown-region" for f in warnings))

    def test_null_region_status_is_allowed(self) -> None:
        front = _minimal_view_front_matter()
        front["regions"] = {"us": None}
        path = self._write(front)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_no_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("# View without front matter\n")
            path = Path(tmp.name)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "view.no-front-matter")

    def test_front_matter_non_mapping_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("---\n- 1\n- 2\n---\n# body\n")
            path = Path(tmp.name)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "view.front-matter-non-mapping")

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            # invalid YAML: unbalanced quote inside a flow mapping
            tmp.write('---\nsectors: {"機械": "neutral\n---\n# body\n')
            path = Path(tmp.name)
        try:
            findings = validate_view_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        # Either invalid-yaml or no-front-matter (regex may not match) is acceptable;
        # both indicate the parser refused malformed input.
        self.assertTrue(
            codes & {"view.invalid-yaml", "view.no-front-matter"},
            f"expected parser failure code, got {codes}",
        )

    def test_repository_view_files_pass(self) -> None:
        repo_view = ROOT / "view"
        files = discover_view_files(repo_view)
        if not files:
            self.skipTest("no view files under repository root")
        for path in files:
            findings = [f for f in validate_view_file(path) if f.severity == "error"]
            self.assertEqual(findings, [], f"view {path} produced error findings: {findings}")


if __name__ == "__main__":
    unittest.main()
