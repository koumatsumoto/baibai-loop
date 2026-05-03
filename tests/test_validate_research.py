from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.research import (
    discover_research_files,
    validate_research_collection,
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
        "decision": "accepted",
        "market_cap_oku": 600,
        "sector_33": "情報・通信業",
        "candidates_ref": "records/03-candidates/2026/04/2026-04-24.yaml",
        "outlook_ref": "records/02-outlook/2026/04/outlook-2026-04-24-bootstrap.yaml",
        "brief_refs": [],
        "ai-draft": True,
        "published_at": "2026-04-25T22:00:00+09:00",
        "tradable_at": "2026-05-15T09:00:00+09:00",
        "macro_gate": "neutral",
        "position_size_oku": 0.01,
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
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_missing_required_field_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["macro_gate"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.missing-field", codes)

    def test_missing_decision_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["decision"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.missing-field", {f.code for f in findings})

    def test_unknown_decision_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["decision"] = "maybe"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.invalid-decision", {f.code for f in findings})

    def test_headwind_accepted_without_override_is_error(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_gate"] = "headwind"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.headwind-without-override", {f.code for f in findings})

    def test_headwind_accepted_with_override_is_warning(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_gate"] = "headwind"
        front["macro_gate_override"] = "event-specific mispricing"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        warning_codes = {f.code for f in findings if f.severity == "warning"}
        error_codes = {f.code for f in findings if f.severity == "error"}
        self.assertIn("research.headwind-with-override", warning_codes)
        self.assertNotIn("research.headwind-without-override", error_codes)

    def test_missing_position_size_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["position_size_oku"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.missing-field", {f.code for f in findings})

    def test_missing_market_cap_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["market_cap_oku"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.missing-field", {f.code for f in findings})

    def test_missing_sector_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        del front["sector_33"]
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.missing-field", {f.code for f in findings})

    def test_low_cap_mean_reversion_accepted_is_error(self) -> None:
        front = _minimal_research_front_matter()
        front["market_cap_oku"] = 250
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.low-cap-mean-reversion", {f.code for f in findings})

    def test_low_cap_mean_reversion_with_override_is_warning(self) -> None:
        front = _minimal_research_front_matter()
        front["market_cap_oku"] = 250
        front["macro_gate_override"] = "catalyst quality offsets low-cap risk"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        warning_codes = {f.code for f in findings if f.severity == "warning"}
        error_codes = {f.code for f in findings if f.severity == "error"}
        self.assertIn("research.low-cap-mean-reversion", warning_codes)
        self.assertNotIn("research.low-cap-mean-reversion", error_codes)

    def test_low_cap_catalyst_playbook_passes_tier_rule(self) -> None:
        front = _minimal_research_front_matter()
        front["market_cap_oku"] = 250
        front["playbook"] = "valuation-catalyst-confirmation-v1"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertNotIn("research.low-cap-mean-reversion", {f.code for f in findings})

    def test_low_cap_skipped_mean_reversion_passes_tier_rule(self) -> None:
        front = _minimal_research_front_matter()
        front["market_cap_oku"] = 250
        front["decision"] = "skipped"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertNotIn("research.low-cap-mean-reversion", {f.code for f in findings})

    def test_mid_cap_mean_reversion_passes_tier_rule(self) -> None:
        front = _minimal_research_front_matter()
        front["market_cap_oku"] = 600
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertNotIn("research.low-cap-mean-reversion", {f.code for f in findings})

    def test_tier_rule_includes_200_and_excludes_500_boundary(self) -> None:
        front_199 = _minimal_research_front_matter()
        front_199["market_cap_oku"] = 199
        front_200 = _minimal_research_front_matter()
        front_200["market_cap_oku"] = 200
        front_499 = _minimal_research_front_matter()
        front_499["market_cap_oku"] = 499
        front_500 = _minimal_research_front_matter()
        front_500["market_cap_oku"] = 500
        paths = [self._write(front) for front in (front_199, front_200, front_499, front_500)]
        try:
            codes_by_cap = []
            for path in paths:
                findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
                codes_by_cap.append({f.code for f in findings})
        finally:
            for path in paths:
                path.unlink()
        self.assertNotIn("research.low-cap-mean-reversion", codes_by_cap[0])
        self.assertIn("research.low-cap-mean-reversion", codes_by_cap[1])
        self.assertIn("research.low-cap-mean-reversion", codes_by_cap[2])
        self.assertNotIn("research.low-cap-mean-reversion", codes_by_cap[3])

    def test_adv_participation_at_cap_is_rejected(self) -> None:
        front = _minimal_research_front_matter()
        front["adv_participation_pct"] = 5.0
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.adv-participation-cap", {f.code for f in findings})

    def test_adv_participation_below_cap_passes(self) -> None:
        front = _minimal_research_front_matter()
        front["adv_participation_pct"] = 4.9
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertNotIn("research.adv-participation-cap", {f.code for f in findings})

    def test_adv_participation_consistent_with_position_and_turnover_passes(self) -> None:
        front = _minimal_research_front_matter()
        front["position_size_oku"] = 0.005
        front["avg_turnover_oku"] = 85.4
        # 0.005 / 85.4 * 100 = 0.005853...
        front["adv_participation_pct"] = 0.00585
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertNotIn("research.adv-participation-inconsistent", {f.code for f in findings})

    def test_adv_participation_100x_off_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["position_size_oku"] = 0.005
        front["avg_turnover_oku"] = 85.4
        # 100 倍ズレた値 (0.585 vs 正解 0.00585)
        front["adv_participation_pct"] = 0.585
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertIn("research.adv-participation-inconsistent", {f.code for f in findings})

    def test_sector_concentration_warns_for_three_accepted_packets(self) -> None:
        base = _minimal_research_front_matter()
        paths_with_front = [
            (Path(f"research-{index}.md"), {**base, "ticker": f"13{index}A"}) for index in range(3)
        ]
        findings = validate_research_collection(paths_with_front)
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.code for f in findings}, {"research.sector-concentration"})

    def test_sector_concentration_ignores_two_accepted_packets(self) -> None:
        base = _minimal_research_front_matter()
        findings = validate_research_collection(
            [
                (Path("research-1.md"), {**base, "ticker": "130A"}),
                (Path("research-2.md"), {**base, "ticker": "131A"}),
            ]
        )
        self.assertEqual(findings, [])

    def test_sector_concentration_ignores_skipped_packets(self) -> None:
        base = _minimal_research_front_matter()
        findings = validate_research_collection(
            [
                (Path("research-1.md"), {**base, "ticker": "130A"}),
                (Path("research-2.md"), {**base, "ticker": "131A"}),
                (
                    Path("research-3.md"),
                    {**base, "ticker": "132A", "decision": "skipped"},
                ),
            ]
        )
        self.assertEqual(findings, [])

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["ticker"] = "abc"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.invalid-ticker", codes)

    def test_unknown_playbook_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["playbook"] = "unknown-playbook"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.unknown-playbook", codes)

    def test_invalid_macro_gate_is_flagged(self) -> None:
        front = _minimal_research_front_matter()
        front["macro_gate"] = "wrong"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.invalid-macro-gate", codes)

    def test_candidates_ref_must_be_yaml(self) -> None:
        front = _minimal_research_front_matter()
        front["candidates_ref"] = "records/03-candidates/2026/04/2026-04-24.md"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.candidates-ref-not-yaml", codes)

    def test_outlook_ref_must_be_yaml(self) -> None:
        front = _minimal_research_front_matter()
        front["outlook_ref"] = "records/02-outlook/2026/04/outlook.md"
        path = self._write(front)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.outlook-ref-not-yaml", codes)

    def test_missing_required_section_is_flagged(self) -> None:
        body = "# Research\n\n## 1. Thesis\nonly thesis\n"
        path = self._write(_minimal_research_front_matter(), body=body)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
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
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
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
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "research.front-matter-non-mapping")

    def test_missing_playbook_schema_at_load_time_is_handled(self) -> None:
        # discover_playbook_schemas が playbook を返すが、load_playbook_schema を
        # 呼んだ時点で schema YAML が無いケース (validate 実行中に削除された等の
        # 競合状態)。uncaught FileNotFoundError で die せず finding に変換される
        # ことを確認する。
        front = _minimal_research_front_matter()
        path = self._write(front)
        try:
            with (
                tempfile.TemporaryDirectory() as empty_root,
                patch(
                    "baibai_loop.validate.research.discover_playbook_schemas",
                    return_value={"valuation-mean-reversion-v1"},
                ),
            ):
                findings = validate_research_file(path, playbooks_root=Path(empty_root))
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("research.missing-playbook-schema", codes)

    def test_invalid_yaml_front_matter_is_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write('---\nticker: "2767\n---\n# body\n')
            path = Path(tmp.name)
        try:
            findings = validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
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
                for f in validate_research_file(path, playbooks_root=ROOT / "records/_playbooks")
                if f.severity == "error"
            ]
            self.assertEqual(findings, [], f"research {path} produced error findings: {findings}")


if __name__ == "__main__":
    unittest.main()
