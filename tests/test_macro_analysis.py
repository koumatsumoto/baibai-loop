from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from baibai_loop.macro_analysis import (
    MACRO_ANALYSIS_ROOT,
    load_macro_analysis,
    risk_posture_sizing_factor,
    sector_lever,
)
from baibai_loop.validate.macro_analysis import validate_macro_analysis_file

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_RECORD = (
    _REPO_ROOT
    / MACRO_ANALYSIS_ROOT
    / "2026"
    / "06"
    / "macro-analysis-2026-06-22-jp-equities-1m.yaml"
)


def _valid_payload() -> dict[str, object]:
    return {
        "kind": "macro-analysis",
        "analysis_id": "macro-analysis-2026-06-22-test",
        "as_of": "2026-06-22",
        "published_at": "2026-06-22T09:00:00+09:00",
        "question": "test question",
        "summary": "test summary",
        "horizon": "1m",
        "data_inputs": [
            {
                "series_id": "jp.nikkei225",
                "window": "2026-05-20..2026-06-19",
                "observation": "59804->71250",
                "used_for": "trend",
            }
        ],
        "sources": [],
        "scenarios": [{"name": "base", "narrative": "base case"}],
        "risks": ["overstretched"],
        "forward_view": "favor mean reversion",
        "risk_posture": {"stance": "neutral", "rationale": "froth signals"},
        "trade_levers": [
            {
                "lever": "sector_tilt",
                "target": "輸送用機器",
                "direction": "favor",
                "rationale": "weak yen tailwind",
            }
        ],
        "confidence": "medium",
    }


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


class MacroAnalysisModelTests(unittest.TestCase):
    def test_load_real_record_parses_levers_and_posture(self) -> None:
        analysis = load_macro_analysis(_REAL_RECORD)

        self.assertEqual(analysis.analysis_id, "macro-analysis-2026-06-22-jp-equities-1m")
        self.assertEqual(analysis.risk_posture.stance, "neutral")
        self.assertEqual(analysis.risk_posture.cash_floor_pct, 15.0)
        self.assertTrue(any(lever.lever == "theme" for lever in analysis.trade_levers))

    def test_sector_lever_matches_target_sector(self) -> None:
        analysis = load_macro_analysis(_REAL_RECORD)

        lever = sector_lever(analysis, "輸送用機器")
        self.assertIsNotNone(lever)
        assert lever is not None
        self.assertEqual(lever.direction, "favor")
        self.assertIsNone(sector_lever(analysis, "存在しない業種"))

    def test_risk_posture_sizing_factor_maps_stance(self) -> None:
        analysis = load_macro_analysis(_REAL_RECORD)

        self.assertEqual(risk_posture_sizing_factor(analysis.risk_posture), 0.75)


class MacroAnalysisValidatorTests(unittest.TestCase):
    def test_valid_record_has_no_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "macro-analysis-2026-06-22-test.yaml"
            _write(path, _valid_payload())

            findings = validate_macro_analysis_file(path)

            self.assertEqual([f for f in findings if f.severity == "error"], [])

    def test_missing_trade_levers_is_error(self) -> None:
        payload = _valid_payload()
        del payload["trade_levers"]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "macro-analysis-2026-06-22-test.yaml"
            _write(path, payload)

            findings = validate_macro_analysis_file(path)

            self.assertTrue(any(f.severity == "error" for f in findings))

    def test_empty_trade_levers_is_error(self) -> None:
        payload = _valid_payload()
        payload["trade_levers"] = []
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "macro-analysis-2026-06-22-test.yaml"
            _write(path, payload)

            findings = validate_macro_analysis_file(path)

            self.assertTrue(any(f.severity == "error" for f in findings))

    def test_unknown_data_input_series_is_warning(self) -> None:
        payload = _valid_payload()
        payload["data_inputs"] = [
            {
                "series_id": "not.a.real.series",
                "window": "w",
                "observation": "o",
                "used_for": "u",
            }
        ]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "macro-analysis-2026-06-22-test.yaml"
            _write(path, payload)

            findings = validate_macro_analysis_file(path)

            self.assertTrue(
                any(f.code == "macro-analysis.unknown-series" for f in findings),
            )


if __name__ == "__main__":
    unittest.main()
