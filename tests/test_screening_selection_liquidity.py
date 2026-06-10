from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.selection import (
    build_selection_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 6, 8)


def _candidate(ticker: str, **overrides: object) -> Mapping[str, object]:
    base: dict[str, object] = {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": "機械",
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "evidence_hits": [{"name": "cashflow-yield-discount"}],
        "metrics": {"ocf_yield": 0.11},
    }
    base.update(overrides)
    return base


class SelectionLiquidityFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)

    def _payload(self, candidates: list[Mapping[str, object]]) -> dict[str, object]:
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
        )

    def _tickers(self, payload: dict[str, object]) -> set[str]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return {item["ticker"] for item in recommendations}

    def _diag(self, payload: dict[str, object]) -> Mapping[str, object]:
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        diagnostics = selection["diagnostics"]
        assert isinstance(diagnostics, Mapping)
        return diagnostics

    def test_filter_excludes_small_thin_recent_and_flagged(self) -> None:
        payload = self._payload(
            [
                _candidate("1111"),
                _candidate("2222", market_cap_oku=99, sector_33="電気機器"),
                _candidate("3333", avg_turnover_oku=0.5, sector_33="小売業"),
                _candidate("4444", listing_span_days=100, sector_33="サービス業"),
                _candidate("5555", jpx_flags=["整理銘柄"], sector_33="化学"),
            ]
        )
        self.assertEqual(self._tickers(payload), {"1111"})
        diagnostics = self._diag(payload)
        self.assertEqual(diagnostics["liquidity_excluded_count"], 4)
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        counts = selection["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["input"], 5)
        self.assertEqual(counts["after_liquidity_filter"], 1)

    def test_missing_facts_pass_but_are_counted(self) -> None:
        payload = self._payload(
            [
                _candidate(
                    "1111",
                    market_cap_oku=None,
                    avg_turnover_oku=None,
                    listing_span_days=None,
                    jpx_flags=[],
                )
            ]
        )
        self.assertEqual(self._tickers(payload), {"1111"})
        self.assertEqual(self._diag(payload)["liquidity_fact_missing_count"], 1)

    def test_profile_config_can_relax_liquidity(self) -> None:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(candidate_record_from_mapping(_candidate("2222", market_cap_oku=50)),),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            profile_overrides={"balanced": {"liquidity": {"min_market_cap_oku": 10}}},
        )
        self.assertEqual(self._tickers(payload), {"2222"})


if __name__ == "__main__":
    unittest.main()


class LiquidityPredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.liquidity = load_screening_rules(DEFAULT_RULES_PATH).selection.liquidity
        self.required = frozenset({"整理銘柄", "特別注意銘柄"})

    def _matches(self, *, require_facts: bool, **overrides: object) -> bool:
        facts: dict[str, object] = {
            "market_cap_oku": 300.0,
            "avg_turnover_oku": 2.0,
            "listing_span_days": 1200,
            "jpx_flags": (),
        }
        facts.update(overrides)
        return self.liquidity.matches(
            market_cap_oku=facts["market_cap_oku"],
            avg_turnover_oku=facts["avg_turnover_oku"],
            listing_span_days=facts["listing_span_days"],
            jpx_flags=facts["jpx_flags"],
            required_jpx_flags=self.required,
            require_facts=require_facts,
        )

    def test_boundary_values_pass(self) -> None:
        self.assertTrue(
            self._matches(
                require_facts=True,
                market_cap_oku=100.0,
                avg_turnover_oku=1.0,
                listing_span_days=182,
            )
        )

    def test_require_facts_excludes_any_missing_fact(self) -> None:
        for field in ("market_cap_oku", "avg_turnover_oku", "listing_span_days", "jpx_flags"):
            with self.subTest(field=field):
                self.assertFalse(self._matches(require_facts=True, **{field: None}))
                self.assertTrue(self._matches(require_facts=False, **{field: None}))

    def test_flagged_ticker_excluded_when_toggle_on(self) -> None:
        self.assertFalse(self._matches(require_facts=True, jpx_flags=("整理銘柄",)))

    def test_flag_outside_required_set_passes(self) -> None:
        self.assertTrue(self._matches(require_facts=True, jpx_flags=("日々公表",)))

    def test_toggle_off_keeps_flagged_ticker(self) -> None:
        relaxed = self.liquidity.model_copy(update={"exclude_jpx_flagged": False})
        self.assertTrue(
            relaxed.matches(
                market_cap_oku=300.0,
                avg_turnover_oku=2.0,
                listing_span_days=1200,
                jpx_flags=("整理銘柄",),
                required_jpx_flags=self.required,
                require_facts=True,
            )
        )


class StabilizationRankTests(unittest.TestCase):
    """Two fast-eligible candidates in the same lane: the one whose latest
    session held flat-or-up outranks the one still falling."""

    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)

    def _fast(self, ticker: str, price_change_1d: float, sector: str) -> Mapping[str, object]:
        return _candidate(
            ticker,
            sector_33=sector,
            price_change_1d=price_change_1d,
            price_change_5d=-0.10,
            price_change_20d=-0.12,
            evidence_hits=[{"name": "cashflow-yield-discount"}],
            metrics={"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3},
        )

    def _tickers(self, candidates: list[Mapping[str, object]]) -> list[str]:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return [item["ticker"] for item in recommendations]

    def test_stabilized_fast_candidate_ranks_first(self) -> None:
        falling = self._fast("1111", price_change_1d=-0.03, sector="機械")
        stabilized = self._fast("2222", price_change_1d=0.01, sector="電気機器")
        self.assertEqual(self._tickers([falling, stabilized])[0], "2222")

    def test_non_fast_candidates_unaffected_by_stabilization(self) -> None:
        calm_a = _candidate("3333", sector_33="機械", price_change_1d=-0.01)
        calm_b = _candidate("4444", sector_33="電気機器", price_change_1d=0.02)
        # Neither is fast-eligible; ordering falls through to the ticker
        # tiebreaker, not the stabilization signal.
        self.assertEqual(self._tickers([calm_a, calm_b]), ["3333", "4444"])
