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

from baibai_loop.validate.screened import (
    discover_screened_files,
    validate_screened_file,
)


def _minimal_screened() -> dict[str, object]:
    return {
        "run_date": "2026-04-24",
        "asof_date": "2026-04-24",
        "universe_size": 100,
        "filters": {
            "min_market_cap_oku": 300,
            "min_avg_turnover_oku": 2.0,
            "exclude_listed_under_months": 6,
        },
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-04-24T09:00:00+09:00",
        "run_id": "screening-20260424-a1b2c3d4",
        "config_hash": "a1b2c3d4e5f6a7b8",
        "cache_manifest_hash": "9988776655443322",
        "tickers": [
            {
                "ticker": "130A",
                "name": "Sample Co",
                "sector_33": "情報・通信業",
                "ttm_quality": {
                    "ev_ebitda": "exact",
                    "p_s": "approximated",
                    "pcfr": "unavailable",
                },
                "threshold_hit": ["sector_median_under_20pct_and_self_range_bottom_20pct"],
            }
        ],
    }


class ScreenedValidationTests(unittest.TestCase):
    def _write(self, payload: object) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            yaml.safe_dump(payload, tmp, allow_unicode=True, sort_keys=False)
            return Path(tmp.name)

    def test_minimal_valid_payload_has_no_findings(self) -> None:
        path = self._write(_minimal_screened())
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_invalid_run_id_pattern_is_flagged(self) -> None:
        payload = _minimal_screened()
        payload["run_id"] = "screening-20260424-XYZ"
        path = self._write(payload)
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        locations = {finding.location for finding in findings}
        self.assertIn("screened.pattern", codes)
        self.assertIn("run_id", locations)

    def test_short_config_hash_is_flagged(self) -> None:
        payload = _minimal_screened()
        payload["config_hash"] = "abc"
        path = self._write(payload)
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        locations = {finding.location for finding in findings}
        self.assertIn("config_hash", locations)

    def test_missing_required_lineage_field_is_flagged(self) -> None:
        payload = _minimal_screened()
        del payload["cache_manifest_hash"]
        path = self._write(payload)
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("screened.required", codes)

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        payload = _minimal_screened()
        tickers = payload["tickers"]
        assert isinstance(tickers, list)
        ticker_entry = tickers[0]
        assert isinstance(ticker_entry, dict)
        ticker_entry["ticker"] = "abc"
        path = self._write(payload)
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        locations = {finding.location for finding in findings}
        self.assertTrue(any("ticker" in loc for loc in locations if loc is not None))

    def test_empty_threshold_hit_is_flagged(self) -> None:
        payload = _minimal_screened()
        tickers = payload["tickers"]
        assert isinstance(tickers, list)
        ticker_entry = tickers[0]
        assert isinstance(ticker_entry, dict)
        ticker_entry["threshold_hit"] = []
        path = self._write(payload)
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertTrue(any(code.startswith("screened.") for code in codes))

    def test_non_mapping_yaml_root_returns_single_finding(self) -> None:
        path = self._write([_minimal_screened()])
        try:
            findings = validate_screened_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "screened.non-mapping")

    def test_repository_screened_files_pass(self) -> None:
        repo_screened = ROOT / "screened"
        files = discover_screened_files(repo_screened)
        if not files:
            self.skipTest("no screened files under repository root")
        for path in files:
            findings = validate_screened_file(path)
            self.assertEqual(findings, [], f"screened YAML {path} produced findings: {findings}")


if __name__ == "__main__":
    unittest.main()
