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

from baibai_loop.validate.outlook import (
    discover_outlook_files,
    validate_outlook_file,
)

_TSE_33_SECTORS: tuple[str, ...] = (
    "水産・農林業",
    "鉱業",
    "建設業",
    "食料品",
    "繊維製品",
    "パルプ・紙",
    "化学",
    "医薬品",
    "石油・石炭製品",
    "ゴム製品",
    "ガラス・土石製品",
    "鉄鋼",
    "非鉄金属",
    "金属製品",
    "機械",
    "電気機器",
    "輸送用機器",
    "精密機器",
    "その他製品",
    "電気・ガス業",
    "陸運業",
    "海運業",
    "空運業",
    "倉庫・運輸関連業",
    "情報・通信業",
    "卸売業",
    "小売業",
    "銀行業",
    "証券、商品先物取引業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
)


def _judgement(status: object) -> dict[str, object]:
    return {"status": status, "rationale": "bootstrap neutral", "source_refs": []}


def _minimal_outlook() -> dict[str, object]:
    return {
        "schema_version": 1,
        "ai_draft": True,
        "published_at": "2026-04-27T09:00:00+09:00",
        "horizon": "1-6m",
        "updated_from": ["records/02-brief/2026/04/2026-04-19-world-weekly-x.yaml"],
        "summary": "summary",
        "sectors": {sector: _judgement("neutral") for sector in _TSE_33_SECTORS},
        "exposure_buckets": {
            "us": _judgement("neutral"),
            "japan-domestic": _judgement("neutral"),
            "japan-external-demand": _judgement("supportive"),
            "emerging": _judgement(None),
        },
        "changes": [],
        "next_triggers": [{"date": "2026-05-12", "text": "BoJ minutes"}],
    }


def _write_yaml(payload: object) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.safe_dump(payload, tmp, allow_unicode=True, sort_keys=False)
        return Path(tmp.name)


class OutlookValidationTests(unittest.TestCase):
    def test_minimal_valid_outlook_has_no_findings(self) -> None:
        path = _write_yaml(_minimal_outlook())
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_missing_required_sector_is_flagged(self) -> None:
        payload = _minimal_outlook()
        sectors = payload["sectors"]
        assert isinstance(sectors, dict)
        del sectors["機械"]
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("outlook.required", codes)

    def test_invalid_status_is_flagged(self) -> None:
        payload = _minimal_outlook()
        sectors = payload["sectors"]
        assert isinstance(sectors, dict)
        sectors["機械"] = {"status": "WRONG", "rationale": "x", "source_refs": []}
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertTrue(any(c.startswith("outlook.") for c in codes))

    def test_null_status_with_rationale_is_allowed(self) -> None:
        payload = _minimal_outlook()
        exposure_buckets = payload["exposure_buckets"]
        assert isinstance(exposure_buckets, dict)
        exposure_buckets["emerging"] = {
            "status": None,
            "rationale": "材料不足",
            "source_refs": [],
        }
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_missing_rationale_is_flagged(self) -> None:
        payload = _minimal_outlook()
        exposure_buckets = payload["exposure_buckets"]
        assert isinstance(exposure_buckets, dict)
        exposure_buckets["us"] = {"status": "neutral", "source_refs": []}
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("outlook.required", codes)

    def test_unknown_exposure_bucket_is_flagged(self) -> None:
        payload = _minimal_outlook()
        exposure_buckets = payload["exposure_buckets"]
        assert isinstance(exposure_buckets, dict)
        exposure_buckets["unknown-exposure"] = _judgement("neutral")
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("outlook.additionalProperties", codes)

    def test_updated_from_must_point_to_yaml(self) -> None:
        payload = _minimal_outlook()
        payload["updated_from"] = ["records/02-brief/2026/04/2026-04-19-world-weekly-x.md"]
        path = _write_yaml(payload)
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        codes = {f.code for f in findings}
        self.assertIn("outlook.pattern", codes)

    def test_repository_outlook_files_pass(self) -> None:
        repo_outlook = ROOT / "records/03-outlook"
        files = discover_outlook_files(repo_outlook)
        if not files:
            self.skipTest("no outlook files under repository root")
        for path in files:
            findings = [f for f in validate_outlook_file(path) if f.severity == "error"]
            self.assertEqual(findings, [], f"outlook {path} produced error findings: {findings}")

    def test_non_mapping_yaml_root_is_flagged(self) -> None:
        path = _write_yaml([_minimal_outlook()])
        try:
            findings = validate_outlook_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "outlook.non-mapping")


if __name__ == "__main__":
    unittest.main()
