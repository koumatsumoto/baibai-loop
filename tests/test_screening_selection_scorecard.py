from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.selection import (
    StructuralOutlook,
    StructuralOutlookConfig,
    build_scorecard_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 6, 12)


def _config() -> StructuralOutlookConfig:
    return StructuralOutlookConfig.model_validate(
        {
            "version": "test",
            "sector_defaults": {"電気機器": "ai_tailwind", "機械": "ai_tailwind"},
            "ticker_overrides": {"9999": {"outlook": "structural_decline", "note": "遊技機"}},
        }
    )


def _candidate(ticker: str, sector_33: str, **overrides: object) -> Mapping[str, object]:
    base: dict[str, object] = {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": sector_33,
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "per_trailing": 11.0,
        "pbr": 0.9,
        "evidence_hits": [{"name": "cashflow-yield-discount"}],
        "metrics": {"ocf_yield": 0.12, "equity_ratio": 0.6, "net_cash_to_market_cap": 0.25},
    }
    base.update(overrides)
    return base


class ScorecardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)
        self.config = _config()

    def _payload(
        self,
        candidates: list[Mapping[str, object]],
        *,
        include_outlooks: frozenset[StructuralOutlook] = frozenset(
            {StructuralOutlook.AI_TAILWIND, StructuralOutlook.NEUTRAL}
        ),
        exclude_tickers: frozenset[str] = frozenset(),
        top: int = 12,
    ) -> dict[str, object]:
        return build_scorecard_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            rules=self.rules,
            structural_config=self.config,
            top=top,
            candidates_ref="test.yaml",
            include_outlooks=include_outlooks,
            exclude_tickers=exclude_tickers,
        )

    def _shortlist(self, payload: dict[str, object]) -> list[Mapping[str, object]]:
        shortlist = payload["shortlist"]
        assert isinstance(shortlist, list)
        return shortlist

    def test_ai_only_drops_neutral_and_decline(self) -> None:
        payload = self._payload(
            [
                _candidate("8035", "電気機器"),
                _candidate("1234", "水産・農林業"),
                _candidate("9999", "機械"),
            ],
            include_outlooks=frozenset({StructuralOutlook.AI_TAILWIND}),
        )
        tickers = [item["ticker"] for item in self._shortlist(payload)]
        self.assertEqual(tickers, ["8035"])
        scorecard = payload["scorecard"]
        assert isinstance(scorecard, Mapping)
        counts = scorecard["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["excluded_outlook"], 2)

    def test_ai_tailwind_ranks_before_neutral_when_both_included(self) -> None:
        payload = self._payload(
            [
                _candidate("1234", "水産・農林業"),
                _candidate("8035", "電気機器"),
            ]
        )
        tickers = [item["ticker"] for item in self._shortlist(payload)]
        self.assertEqual(tickers[0], "8035")
        self.assertIn("1234", tickers)

    def test_exclude_tickers_drops_holdings(self) -> None:
        payload = self._payload(
            [_candidate("8035", "電気機器"), _candidate("6857", "電気機器")],
            include_outlooks=frozenset({StructuralOutlook.AI_TAILWIND}),
            exclude_tickers=frozenset({"6857"}),
        )
        tickers = [item["ticker"] for item in self._shortlist(payload)]
        self.assertEqual(tickers, ["8035"])
        scorecard = payload["scorecard"]
        assert isinstance(scorecard, Mapping)
        counts = scorecard["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["excluded_held"], 1)

    def test_entry_exposes_axes_and_structural(self) -> None:
        payload = self._payload([_candidate("8035", "電気機器")])
        entry = self._shortlist(payload)[0]
        self.assertEqual(entry["structural_outlook"], "ai_tailwind")
        axes = entry["axes"]
        assert isinstance(axes, Mapping)
        for axis in (
            "valuation_discount",
            "cashflow_durability",
            "balance_sheet",
            "dislocation",
            "long_hold",
            "structural",
        ):
            self.assertIn(axis, axes)
        valuation = axes["valuation_discount"]
        assert isinstance(valuation, Mapping)
        self.assertEqual(valuation["per_trailing"], 11.0)

    def test_illiquid_candidate_excluded(self) -> None:
        payload = self._payload(
            [_candidate("8035", "電気機器", avg_turnover_oku=0.2)],
            include_outlooks=frozenset({StructuralOutlook.AI_TAILWIND}),
        )
        self.assertEqual(self._shortlist(payload), [])
        scorecard = payload["scorecard"]
        assert isinstance(scorecard, Mapping)
        counts = scorecard["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["excluded_liquidity"], 1)


if __name__ == "__main__":
    unittest.main()
