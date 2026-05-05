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

from baibai_loop.validate.candidates import (
    discover_candidates_files,
    validate_candidates_file,
)


def _minimal_candidates() -> dict[str, object]:
    return {
        "run_date": "2026-04-24",
        "asof_date": "2026-04-24",
        "universe_size": 100,
        "filters": {
            "min_market_cap_oku": 200,
            "min_avg_turnover_oku": 3.0,
            "exclude_listed_under_days": 182,
        },
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-04-24T09:00:00+09:00",
        "run_id": "screening-20260424-a1b2c3d4",
        "config_hash": "a1b2c3d4e5f6a7b8",
        "cache_manifest_hash": "9988776655443322",
        "candidates": [
            {
                "ticker": "130A",
                "name": "Sample Co",
                "sector_33": "情報・通信業",
                "metrics": {},
                "ttm_quality": {
                    "ev_ebitda": "exact",
                    "p_s": "approximated",
                    "pcfr": "unavailable",
                    "ocf_yield": "unavailable",
                    "sales": "approximated",
                    "fcf_yield": "unavailable",
                    "net_cash": "unavailable",
                },
                "signals": [
                    {
                        "name": "valuation-reversion",
                        "playbook": "valuation-reversion",
                        "reasons": ["sector_median_discount_and_self_range_bottom"],
                        "metrics": {},
                    }
                ],
            }
        ],
    }


class CandidatesValidationTests(unittest.TestCase):
    def _write(self, payload: object) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            yaml.safe_dump(payload, tmp, allow_unicode=True, sort_keys=False)
            return Path(tmp.name)

    def test_minimal_valid_payload_has_no_findings(self) -> None:
        path = self._write(_minimal_candidates())
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        self.assertEqual(findings, [])

    def test_invalid_run_id_pattern_is_flagged(self) -> None:
        payload = _minimal_candidates()
        payload["run_id"] = "screening-20260424-XYZ"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        locations = {finding.location for finding in findings}
        self.assertIn("candidates.pattern", codes)
        self.assertIn("run_id", locations)

    def test_short_config_hash_is_flagged(self) -> None:
        payload = _minimal_candidates()
        payload["config_hash"] = "abc"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        locations = {finding.location for finding in findings}
        self.assertIn("config_hash", locations)

    def test_missing_required_lineage_field_is_flagged(self) -> None:
        payload = _minimal_candidates()
        del payload["cache_manifest_hash"]
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("candidates.required", codes)

    def test_invalid_ticker_pattern_is_flagged(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["ticker"] = "abc"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        locations = {finding.location for finding in findings}
        self.assertTrue(any("ticker" in loc for loc in locations if loc is not None))

    def test_empty_signals_is_flagged(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["signals"] = []
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertTrue(any(code.startswith("candidates.") for code in codes))

    def test_unknown_root_field_is_flagged(self) -> None:
        payload = _minimal_candidates()
        payload["typo_field_name"] = "oops"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("candidates.additionalProperties", codes)

    def test_unknown_ticker_field_is_flagged(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["typo_field"] = "oops"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertIn("candidates.additionalProperties", codes)

    def test_non_mapping_yaml_root_returns_single_finding(self) -> None:
        path = self._write([_minimal_candidates()])
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "candidates.non-mapping")

    def test_repository_candidates_files_pass(self) -> None:
        repo_candidates = ROOT / "candidates"
        files = discover_candidates_files(repo_candidates)
        if not files:
            self.skipTest("no candidates files under repository root")
        for path in files:
            findings = validate_candidates_file(path)
            self.assertEqual(findings, [], f"candidates YAML {path} produced findings: {findings}")


if __name__ == "__main__":
    unittest.main()
