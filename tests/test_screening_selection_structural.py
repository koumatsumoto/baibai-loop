from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.selection import (
    StructuralOutlook,
    StructuralOutlookConfig,
    build_selection_payload,
    candidate_record_from_mapping,
    classify_structural_outlook,
)

_ASOF = date(2026, 6, 8)


def _config() -> StructuralOutlookConfig:
    return StructuralOutlookConfig.model_validate(
        {
            "version": "test",
            "sector_defaults": {"電気機器": "ai_tailwind", "機械": "ai_tailwind"},
            "ticker_overrides": {
                "9999": {"outlook": "structural_decline", "note": "遊技機"},
                "8888": {"outlook": "ai_tailwind", "note": "半導体商社"},
            },
        }
    )


class ClassifyStructuralOutlookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()

    def test_ticker_override_wins_over_sector_default(self) -> None:
        result = classify_structural_outlook(ticker="9999", sector_33="機械", config=self.config)
        self.assertEqual(result.outlook, StructuralOutlook.STRUCTURAL_DECLINE)
        self.assertEqual(result.basis, "ticker_override")
        self.assertEqual(result.note, "遊技機")

    def test_sector_default_applies_without_override(self) -> None:
        result = classify_structural_outlook(
            ticker="1234", sector_33="電気機器", config=self.config
        )
        self.assertEqual(result.outlook, StructuralOutlook.AI_TAILWIND)
        self.assertEqual(result.basis, "sector_default")

    def test_unmapped_sector_falls_back_to_neutral(self) -> None:
        result = classify_structural_outlook(
            ticker="1234", sector_33="水産・農林業", config=self.config
        )
        self.assertEqual(result.outlook, StructuralOutlook.NEUTRAL)
        self.assertEqual(result.basis, "default")

    def test_ticker_override_can_promote_cross_sector(self) -> None:
        result = classify_structural_outlook(ticker="8888", sector_33="卸売業", config=self.config)
        self.assertEqual(result.outlook, StructuralOutlook.AI_TAILWIND)
        self.assertEqual(result.basis, "ticker_override")


def _candidate(ticker: str, sector_33: str, **overrides: object) -> Mapping[str, object]:
    base: dict[str, object] = {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": sector_33,
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "evidence_hits": [{"name": "cashflow-yield-discount"}],
        "metrics": {"ocf_yield": 0.11},
    }
    base.update(overrides)
    return base


class SelectAnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)

    def _payload(self, *, structural_config: StructuralOutlookConfig | None) -> dict[str, object]:
        candidates = [
            _candidate("8035", "電気機器"),
            _candidate("9999", "機械"),
        ]
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            structural_config=structural_config,
        )

    def _recommendations(self, payload: dict[str, object]) -> list[Mapping[str, object]]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return recommendations

    def test_annotation_present_when_config_supplied(self) -> None:
        payload = self._payload(structural_config=_config())
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        self.assertEqual(by_ticker["8035"]["structural_outlook"], "ai_tailwind")
        self.assertEqual(by_ticker["9999"]["structural_outlook"], "structural_decline")
        self.assertIn("ai_tailwind", by_ticker["8035"]["reason_tags"])
        self.assertIn("structural_decline", by_ticker["9999"]["risk_tags"])

    def test_diagnostics_count_outlooks(self) -> None:
        payload = self._payload(structural_config=_config())
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        diagnostics = selection["diagnostics"]
        assert isinstance(diagnostics, Mapping)
        counts = diagnostics["structural_outlook_counts"]
        self.assertEqual(counts, {"ai_tailwind": 1, "structural_decline": 1})

    def test_annotation_absent_without_config(self) -> None:
        payload = self._payload(structural_config=None)
        for item in self._recommendations(payload):
            self.assertIsNone(item["structural_outlook"])
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        diagnostics = selection["diagnostics"]
        assert isinstance(diagnostics, Mapping)
        self.assertEqual(diagnostics["structural_outlook_counts"], {})


if __name__ == "__main__":
    unittest.main()
