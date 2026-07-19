from __future__ import annotations

import unittest
from pathlib import Path

from baibai_engine.validation.macro_context import _validate_custom

_PATH = Path("records/01-macro-context/2026/06/x.yaml")


class MacroContextSeriesRegistryTests(unittest.TestCase):
    def _unregistered_findings(self, payload: dict[str, object]) -> list[str]:
        return [
            finding.message
            for finding in _validate_custom(_PATH, payload)
            if finding.code == "macro-context.unregistered-indicator-series"
        ]

    def test_unregistered_indicator_series_id_warns(self) -> None:
        payload: dict[str, object] = {
            "inputs": {"indicator_series": [{"series_id": "totally_unregistered_xyz"}]}
        }
        findings = [
            finding
            for finding in _validate_custom(_PATH, payload)
            if finding.code == "macro-context.unregistered-indicator-series"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "warning")
        self.assertIn("totally_unregistered_xyz", findings[0].message)
        self.assertEqual(findings[0].location, "inputs.indicator_series[0].series_id")

    def test_registered_series_id_and_alias_pass(self) -> None:
        # Canonical id and a registered alias both resolve (boj_policy_rate is now an
        # alias of jp.policy_rate), so neither raises the unregistered warning.
        for series_id in ("jp.policy_rate", "boj_policy_rate", "nikkei_225"):
            payload: dict[str, object] = {
                "inputs": {"indicator_series": [{"series_id": series_id}]}
            }
            self.assertEqual(self._unregistered_findings(payload), [], series_id)

    def test_material_delta_requires_resolved_nonfailed_input(self) -> None:
        payload: dict[str, object] = {
            "as_of": "2026-07-01",
            "valid_until": "2026-07-08",
            "published_at": "2026-07-01T09:00:00+09:00",
            "inputs": {
                "articles": [
                    {"input_id": "failed-source", "status": "failed"},
                ],
                "indicator_series": [],
            },
            "material_deltas": [
                {"source_ids": ["failed-source"]},
            ],
            "sizing_cautions": [],
        }

        codes = {finding.code for finding in _validate_custom(_PATH, payload)}

        self.assertIn("macro-context.failed-source-only", codes)

    def test_material_delta_rejects_unknown_input_id(self) -> None:
        payload: dict[str, object] = {
            "inputs": {"articles": [], "indicator_series": []},
            "material_deltas": [{"source_ids": ["missing"]}],
            "sizing_cautions": [],
        }

        codes = {finding.code for finding in _validate_custom(_PATH, payload)}

        self.assertIn("macro-context.unknown-source-id", codes)


if __name__ == "__main__":
    unittest.main()
