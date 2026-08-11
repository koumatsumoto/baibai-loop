from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import (
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
        "metrics": {"ocf_yield": 0.11, "er_annual": 0.05},
    }
    base.update(overrides)
    return base


class SelectionLiquidityFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)

    def _payload(
        self,
        candidates: list[Mapping[str, object]],
        *,
        longlist_top: int = 0,
    ) -> dict[str, object]:
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            longlist_top=longlist_top,
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

    def test_recommendation_surfaces_valuation_downside_and_value_trap_signals(self) -> None:
        # The recommendation summary must carry headline valuation (per/pbr/ev_ebitda/p_s),
        # downside-protection metrics (cash/net-cash/equity/ocf), and the value-trap
        # discriminators (earnings momentum + cash conversion) so triage can reject a
        # cheap trailing PER that masks declining or non-cash earnings, without a
        # separate ticker-profile call per candidate.
        payload = self._payload(
            [
                _candidate(
                    "4839",
                    per_trailing=21.48,
                    pbr=0.4,
                    ev_ebitda=6.5,
                    p_s=0.37,
                    avg_turnover_oku=3.5,
                    price_change_60d=-0.26,
                    metrics={
                        "cash_to_market_cap": 0.93,
                        "equity_ratio": 0.74,
                        "ocf_yield": 0.19,
                        "dps_actual_annual": 40.0,
                        "dps_forecast_annual": 22.0,
                        "er_dividend_yield": 0.04,
                        "er_annual": 0.05,
                        "operating_profit_yoy": -0.28,
                        "sales_yoy": 0.00,
                        "fcf_yield": 0.05,
                    },
                )
            ]
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        rec = next(item for item in recommendations if item["ticker"] == "4839")
        self.assertEqual(rec["per_trailing"], 21.48)
        self.assertEqual(rec["pbr"], 0.4)
        self.assertEqual(rec["ev_ebitda"], 6.5)
        self.assertEqual(rec["p_s"], 0.37)
        self.assertEqual(rec["cash_to_market_cap"], 0.93)
        self.assertEqual(rec["equity_ratio"], 0.74)
        self.assertEqual(rec["ocf_yield"], 0.19)
        # dividend carry triage: actual/forecast DPS の乖離を summary で見られるようにする
        self.assertEqual(rec["dps_actual_annual"], 40.0)
        self.assertEqual(rec["dps_forecast_annual"], 22.0)
        self.assertEqual(rec["er_dividend_yield"], 0.04)
        # value-trap discriminators: declining OP yoy and weak cash conversion
        self.assertEqual(rec["operating_profit_yoy"], -0.28)
        self.assertEqual(rec["sales_yoy"], 0.00)
        self.assertEqual(rec["fcf_yield"], 0.05)
        # liquidity + 60d dislocation depth for order feasibility and oversold read
        self.assertEqual(rec["avg_turnover_oku"], 3.5)
        self.assertEqual(rec["price_change_60d"], -0.26)

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

    def test_missing_facts_are_excluded_but_counted(self) -> None:
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
        self.assertEqual(self._tickers(payload), set())
        self.assertEqual(self._diag(payload)["liquidity_excluded_count"], 1)
        self.assertEqual(self._diag(payload)["liquidity_fact_missing_count"], 1)

    def test_er_ranked_population_includes_candidate_without_evidence_hits(self) -> None:
        payload = self._payload(
            [
                _candidate(
                    "1111",
                    evidence_hits=[],
                    metrics={"er_annual": 0.12, "ocf_yield": 0.05},
                ),
                _candidate(
                    "2222",
                    sector_33="化学",
                    metrics={"er_annual": 0.08, "ocf_yield": 0.11},
                ),
            ]
        )

        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        self.assertEqual([item["ticker"] for item in recommendations], ["1111", "2222"])
        self.assertIsNone(recommendations[0]["selection_playbook"])
        self.assertEqual(payload["selection"]["counts"]["evidence_annotated"], 1)

    def test_financial_sectors_without_evidence_remain_in_er_ranking(self) -> None:
        financial_sectors = (
            "銀行業",
            "証券・商品先物取引業",
            "保険業",
            "その他金融業",
        )
        payload = self._payload(
            [
                _candidate(
                    str(1001 + index),
                    sector_33=sector,
                    evidence_hits=[],
                    metrics={"er_annual": 0.12 - index * 0.01},
                )
                for index, sector in enumerate(financial_sectors)
            ],
            longlist_top=len(financial_sectors),
        )

        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        self.assertEqual(
            [item["sector_33"] for item in recommendations],
            list(financial_sectors),
        )
        self.assertTrue(all(item["selection_playbook"] is None for item in recommendations))
        self.assertEqual(payload["selection"]["counts"]["evidence_annotated"], 0)
        longlist = payload["longlist"]
        assert isinstance(longlist, list)
        self.assertEqual(
            [item["ticker"] for item in longlist],
            [str(1001 + index) for index in range(len(financial_sectors))],
        )
        self.assertTrue(all(item["screening_playbook"] is None for item in longlist))

    def test_playbook_cap_binds_via_profile_override(self) -> None:
        """max_recommended_per_playbook は E[r] 主キー下でも enforcement が生きている。

        既定 rules は 10 (実質無効) だが、override で 1 に絞ると同一 playbook の
        2 本目が推奨から落ちる。"""
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(
                candidate_record_from_mapping(item)
                for item in [
                    _candidate("1111", sector_33="機械"),
                    _candidate("2222", sector_33="化学"),
                ]
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            profile_overrides={"balanced": {"diversity": {"max_recommended_per_playbook": 1}}},
        )
        self.assertEqual(self._tickers(payload), {"1111"})

    def test_playbook_cap_does_not_bind_candidates_without_evidence_hits(self) -> None:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(
                candidate_record_from_mapping(item)
                for item in [
                    _candidate(
                        "1111",
                        sector_33="機械",
                        evidence_hits=[],
                        metrics={"er_annual": 0.07},
                    ),
                    _candidate(
                        "2222",
                        sector_33="化学",
                        evidence_hits=[],
                        metrics={"er_annual": 0.06},
                    ),
                ]
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            profile_overrides={"balanced": {"diversity": {"max_recommended_per_playbook": 1}}},
        )
        self.assertEqual(self._tickers(payload), {"1111", "2222"})

    def test_er_missing_candidates_are_excluded_from_ranking_population(self) -> None:
        payload = self._payload(
            [
                _candidate("1111", metrics={"er_annual": 0.05}),
                _candidate("2222", sector_33="化学", metrics={"er_annual": None}),
            ]
        )

        self.assertEqual(self._tickers(payload), {"1111"})
        counts = payload["selection"]["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["after_er_filter"], 1)
        self.assertEqual(counts["er_missing"], 1)
        # No dividend basis problem here, so the named list stays empty rather than
        # inheriting the count.
        self.assertEqual(counts["er_missing_unresolved_dividend_basis"], [])

    def test_an_unresolvable_dividend_basis_is_named_where_the_candidate_left(self) -> None:
        """The row never reaches the machine table, so the funnel has to say who left.

        A year whose share basis cannot be resolved carries no yield and therefore no
        E[r], which drops the name before ranking. Without the codes here, that name is
        indistinguishable from one the screen found expensive.
        """
        payload = self._payload(
            [
                _candidate("1111", metrics={"er_annual": 0.05}),
                _candidate(
                    "2222",
                    sector_33="化学",
                    metrics={"er_annual": None, "dividend_basis": "unresolved_split_basis"},
                ),
                _candidate("3333", sector_33="機械", metrics={"er_annual": None}),
            ]
        )

        counts = payload["selection"]["counts"]
        assert isinstance(counts, Mapping)
        self.assertEqual(counts["er_missing"], 2)
        self.assertEqual(counts["er_missing_unresolved_dividend_basis"], ["2222"])

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

    def test_supply_demand_gate_only_changes_recommendations_and_passes_missing(self) -> None:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(
                candidate_record_from_mapping(item)
                for item in [
                    _candidate(
                        "1111",
                        metrics={"er_annual": 0.12, "margin_std_long_share": 0.75},
                    ),
                    _candidate(
                        "2222",
                        sector_33="化学",
                        metrics={"er_annual": 0.10, "margin_std_long_share": 0.74},
                    ),
                    _candidate(
                        "3333",
                        sector_33="小売業",
                        metrics={"er_annual": 0.08, "margin_std_long_share": None},
                    ),
                ]
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            longlist_top=3,
            profile_overrides={
                "balanced": {
                    "supply_demand": {
                        "margin_std_long_share_exclude_at_or_above": 0.75,
                    }
                }
            },
        )

        self.assertEqual(self._tickers(payload), {"2222", "3333"})
        longlist = payload["longlist"]
        assert isinstance(longlist, list)
        self.assertEqual([item["ticker"] for item in longlist], ["1111", "2222", "3333"])
        self.assertEqual(self._diag(payload)["supply_demand_excluded_count"], 1)

    def test_short_to_adv_annotation_does_not_change_selection_output(self) -> None:
        candidates = [
            _candidate("1111", metrics={"er_annual": 0.12}),
            _candidate("2222", sector_33="化学", metrics={"er_annual": 0.10}),
        ]
        annotated = [
            {
                **candidate,
                "metrics": {
                    **candidate["metrics"],
                    "margin_week_end": "2026-07-24",
                    "margin_short_to_adv": 40.0 if candidate["ticker"] == "1111" else None,
                },
            }
            for candidate in candidates
        ]

        self.assertEqual(
            self._payload(candidates, longlist_top=2),
            self._payload(annotated, longlist_top=2),
        )


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
