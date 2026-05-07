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

from baibai_loop.validate.brief import (
    discover_brief_files,
    validate_brief_file,
)


def _minimal_world_weekly() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "world-weekly",
        "type": "periodic",
        "scope": "world",
        "ai_draft": True,
        "published_at": "2026-05-03T22:40:00+09:00",
        "observation_date": "2026-05-03",
        "period": {
            "start": "2026-04-27",
            "end": "2026-05-03",
            "market_basis_date": "2026-04-30",
        },
        "sources": [
            {
                "id": "fed-h15",
                "name": "Federal Reserve H.15",
                "url": "https://www.federalreserve.gov/releases/h15/",
                "accessed_at": "2026-05-03",
                "status": "ok",
            }
        ],
        "layers": {
            "world": {
                "market_indicators": [
                    {
                        "name": "米10Y利回り",
                        "value": "4.40%",
                        "as_of": "2026-04-30",
                        "wow_comment": "+9bp",
                        "source_ids": ["fed-h15"],
                    }
                ],
                "events": [],
            },
            "japan": {},
            "japan_equity": {},
        },
        "deltas": {
            "threshold_breaches": [],
            "direction_history": [{"indicator": "米10Y利回り", "history": "↑↓→↑"}],
        },
        "next_events": [{"date": "2026-05-12", "text": "日銀 主な意見公表予定"}],
    }


def _write_yaml(payload: object) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.safe_dump(payload, tmp, allow_unicode=True, sort_keys=False)
        return Path(tmp.name)


class BriefValidationTests(unittest.TestCase):
    def test_minimal_valid_world_weekly_has_no_findings(self) -> None:
        path = _write_yaml(_minimal_world_weekly())
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_world_weekly_requires_deltas(self) -> None:
        payload = _minimal_world_weekly()
        del payload["deltas"]
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("brief.required", codes)

    def test_macro_monthly_requires_month(self) -> None:
        payload = _minimal_world_weekly()
        payload["kind"] = "macro-monthly"
        payload["type"] = "periodic"
        del payload["period"]
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("brief.required", codes)

    def test_unknown_source_id_reference_is_flagged(self) -> None:
        payload = _minimal_world_weekly()
        layers = payload["layers"]
        assert isinstance(layers, dict)
        world = layers["world"]
        assert isinstance(world, dict)
        indicators = world["market_indicators"]
        assert isinstance(indicators, list)
        first = indicators[0]
        assert isinstance(first, dict)
        first["source_ids"] = ["unknown-source"]
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("brief.unknown-source-id", codes)

    def test_invalid_kind_is_flagged(self) -> None:
        payload = _minimal_world_weekly()
        payload["kind"] = "not-a-kind"
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("brief.enum", codes)

    def test_layers_must_have_three_canonical_keys(self) -> None:
        payload = _minimal_world_weekly()
        layers = payload["layers"]
        assert isinstance(layers, dict)
        del layers["japan_equity"]
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("brief.required", codes)

    def test_invalid_status_in_source_is_flagged(self) -> None:
        payload = _minimal_world_weekly()
        sources = payload["sources"]
        assert isinstance(sources, list)
        src = sources[0]
        assert isinstance(src, dict)
        src["status"] = "weird"
        path = _write_yaml(payload)
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertTrue(any(c.startswith("brief.") for c in codes))

    def test_repository_brief_files_pass(self) -> None:
        repo_brief = ROOT / "records/02-brief"
        files = discover_brief_files(repo_brief)
        if not files:
            self.skipTest("no brief files under repository root")
        for path in files:
            findings = [f for f in validate_brief_file(path) if f.severity == "error"]
            self.assertEqual(findings, [], f"brief {path} produced error findings: {findings}")

    def test_non_mapping_yaml_root_is_flagged(self) -> None:
        path = _write_yaml([_minimal_world_weekly()])
        try:
            findings = validate_brief_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "brief.non-mapping")


if __name__ == "__main__":
    unittest.main()
