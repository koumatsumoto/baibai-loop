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
    snapshot = {
        "ref_path": "records/_config/screening-rules/2026-05-01T000000+0900.yaml",
    }
    return {
        "run_date": "2026-05-01",
        "asof_date": "2026-05-01",
        "universe_size": 1347,
        "filters": {
            "min_market_cap_oku": 200,
            "min_avg_turnover_oku": 3.0,
            "exclude_listed_under_days": 182,
        },
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-05-01T09:00:00+09:00",
        "run_id": "screening-20260501",
        "universe_ref": {
            **snapshot,
            "ref_path": "records/_universe-snapshots/2026/05/2026-05-01T192150+0900.yaml",
        },
        "candidates": [
            {
                "ticker": "130A",
                "name": "Sample Co",
                "screen_run_id": "screening-20260501",
                "candidate_id": "candidate-2026-05-01-130A",
                "candidate_key": "screening-20260501:130A",
                "playbook_screen_result": "hit",
                "policy_gate_result": "pass",
                "liquidity_gate_result": "pass",
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
                "evidence_hits": [
                    {
                        "evidence_hit_id": "candidate-2026-05-01-130A-valuation-reversion",
                        "playbook_id": "valuation-reversion",
                        "claim_id": "130A-valuation-reversion",
                        "claim_type": "valuation_reversion",
                        "evidence_family_set": ["valuation"],
                        "decision_role": "sizing_evidence",
                        "evidence_polarity": "supports",
                        "source_status": "ok",
                        "sizing_eligible": True,
                        "independence_component_id": "valuation-reversion",
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
        payload["run_id"] = "screening-20260501-XYZ"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        locations = {finding.location for finding in findings}
        self.assertIn("candidates.pattern", codes)
        self.assertIn("run_id", locations)

    def test_missing_universe_ref_is_flagged(self) -> None:
        payload = _minimal_candidates()
        del payload["universe_ref"]
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        self.assertIn("candidates.required", {finding.code for finding in findings})

    def test_universe_ref_asof_mismatch_is_flagged(self) -> None:
        payload = _minimal_candidates()
        universe_ref = payload["universe_ref"]
        assert isinstance(universe_ref, dict)
        universe_ref["ref_path"] = "records/_universe-snapshots/2026/05/2026-05-08T111721+0900.yaml"
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_universe_ref_size_mismatch_is_flagged_for_non_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 99\n"
                "members_scope: not_recorded\n"
                "members_recorded: 0\n"
                "members: []\n",
                encoding="utf-8",
            )
            payload = _minimal_candidates()
            payload["universe_size"] = 100
            universe_ref = payload["universe_ref"]
            assert isinstance(universe_ref, dict)
            universe_ref["ref_path"] = (
                "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            )
            path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

            findings = validate_candidates_file(path)

        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_canonical_universe_ref_requires_members_to_match_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 100\n"
                "members_scope: full_universe\n"
                "members_recorded: 0\n"
                "members: []\n",
                encoding="utf-8",
            )
            payload = _minimal_candidates()
            payload["universe_size"] = 100
            universe_ref = payload["universe_ref"]
            assert isinstance(universe_ref, dict)
            universe_ref["ref_path"] = (
                "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            )
            path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

            findings = validate_candidates_file(path)

        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_canonical_universe_ref_requires_candidate_ticker_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 100\n"
                "members_scope: full_universe\n"
                "members_recorded: 100\n"
                "members:\n"
                + "\n".join(
                    f"- ticker: '{index:04d}'\n  sector_33: 情報・通信業" for index in range(100)
                )
                + "\n",
                encoding="utf-8",
            )
            payload = _minimal_candidates()
            payload["universe_size"] = 100
            universe_ref = payload["universe_ref"]
            assert isinstance(universe_ref, dict)
            universe_ref["ref_path"] = (
                "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            )
            path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

            findings = validate_candidates_file(path)

        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_canonical_universe_ref_rejects_duplicate_member_tickers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 2\n"
                "members_scope: full_universe\n"
                "members_recorded: 2\n"
                "members:\n"
                "- ticker: '130A'\n"
                "  sector_33: 情報・通信業\n"
                "- ticker: '130A'\n"
                "  sector_33: 情報・通信業\n",
                encoding="utf-8",
            )
            payload = _minimal_candidates()
            payload["universe_size"] = 2
            universe_ref = payload["universe_ref"]
            assert isinstance(universe_ref, dict)
            universe_ref["ref_path"] = (
                "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            )
            path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

            findings = validate_candidates_file(path)

        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_canonical_universe_ref_rejects_unknown_members_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe = root / "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            universe.parent.mkdir(parents=True)
            universe.write_text(
                "snapshot_id: universe-20260501\n"
                "as_of: '2026-05-01'\n"
                "universe_size: 100\n"
                "members_scope: partial\n"
                "members_recorded: 0\n"
                "members: []\n",
                encoding="utf-8",
            )
            payload = _minimal_candidates()
            payload["universe_size"] = 100
            universe_ref = payload["universe_ref"]
            assert isinstance(universe_ref, dict)
            universe_ref["ref_path"] = (
                "records/_universe-snapshots/2026/05/2026-05-01T090000+0900.yaml"
            )
            path = root / "records/04-candidates/2026/05/2026-05-01.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

            findings = validate_candidates_file(path)

        self.assertIn("candidates.universe-ref", {finding.code for finding in findings})

    def test_missing_required_candidates_field_is_flagged(self) -> None:
        payload = _minimal_candidates()
        del payload["candidates"]
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

    def test_empty_evidence_hits_is_flagged(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["evidence_hits"] = []
        path = self._write(payload)
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        codes = {finding.code for finding in findings}
        self.assertTrue(any(code.startswith("candidates.") for code in codes))

    def test_candidate_screen_run_id_must_match_root_run_id(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["screen_run_id"] = "screening-20260501-deadbeef"
        path = self._write(payload)
        try:
            codes = {finding.code for finding in validate_candidates_file(path)}
        finally:
            path.unlink()
        self.assertIn("candidates.screen-run-id", codes)

    def test_candidate_key_must_derive_from_run_id_and_ticker(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["candidate_key"] = "screening-20260501:9999"
        path = self._write(payload)
        try:
            codes = {finding.code for finding in validate_candidates_file(path)}
        finally:
            path.unlink()
        self.assertIn("candidates.candidate-key", codes)

    def test_candidate_id_must_derive_from_asof_date_and_ticker(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        candidate_entry["candidate_id"] = "candidate-screening-20260501-deadbeef-130A"
        path = self._write(payload)
        try:
            codes = {finding.code for finding in validate_candidates_file(path)}
        finally:
            path.unlink()
        self.assertIn("candidates.candidate-id", codes)

    def test_duplicate_candidate_and_evidence_ids_are_flagged(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidates.append(dict(candidates[0]))
        path = self._write(payload)
        try:
            codes = {finding.code for finding in validate_candidates_file(path)}
        finally:
            path.unlink()
        self.assertIn("candidates.duplicate-candidate-id", codes)
        self.assertIn("candidates.duplicate-candidate-key", codes)
        self.assertIn("candidates.duplicate-evidence-hit-id", codes)

    def test_warning_evidence_cannot_be_sizing_eligible(self) -> None:
        payload = _minimal_candidates()
        candidates = payload["candidates"]
        assert isinstance(candidates, list)
        candidate_entry = candidates[0]
        assert isinstance(candidate_entry, dict)
        hits = candidate_entry["evidence_hits"]
        assert isinstance(hits, list)
        hit = hits[0]
        assert isinstance(hit, dict)
        hit["source_status"] = "warning"
        hit["sizing_eligible"] = True
        path = self._write(payload)
        try:
            codes = {finding.code for finding in validate_candidates_file(path)}
        finally:
            path.unlink()
        self.assertIn("candidates.ineligible-source-status", codes)

    def test_non_mapping_yaml_root_returns_single_finding(self) -> None:
        path = self._write([_minimal_candidates()])
        try:
            findings = validate_candidates_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "candidates.non-mapping")

    def test_repository_candidates_files_pass(self) -> None:
        repo_candidates = ROOT / "records/04-candidates"
        files = discover_candidates_files(repo_candidates)
        if not files:
            self.skipTest("no candidates files under repository root")
        for path in files:
            findings = validate_candidates_file(path)
            self.assertEqual(findings, [], f"candidates YAML {path} produced findings: {findings}")


if __name__ == "__main__":
    unittest.main()
