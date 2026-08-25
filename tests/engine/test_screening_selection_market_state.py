from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from tests.helpers.screening_run import evidence_hit

from baibai_engine.screening.cli import build_parser
from baibai_engine.screening.regime import MarketRegime, MarketRegimeSnapshot
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import (
    build_selection_payload,
    build_selection_sweep_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 5, 29)

# Recent heavy decliner. The decline itself must not move the ranking:
# 主キーは E[r] で、market-regime / price-decline annotation は順位を変えない。
_DECLINER_CANDIDATE: Mapping[str, object] = {
    "ticker": "9999",
    "name": "recent decliner",
    "sector_33": "情報・通信業",
    "market_cap_oku": 500,
    "avg_turnover_oku": 2.0,
    "listing_span_days": 1200,
    "jpx_flags": [],
    "price_change_5d": -0.10,
    "price_change_20d": -0.12,
    "evidence_hits": [evidence_hit("sales-discount-growth")],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3, "er_annual": 0.04},
}

# Same fundamentals without the price decline; higher E[r] outranks the decliner.
_CALM_CANDIDATE: Mapping[str, object] = {
    "ticker": "1111",
    "name": "calm value",
    "sector_33": "機械",
    "market_cap_oku": 500,
    "avg_turnover_oku": 2.0,
    "listing_span_days": 1200,
    "jpx_flags": [],
    "price_change_5d": 0.01,
    "price_change_20d": 0.02,
    "evidence_hits": [evidence_hit("valuation-reversion")],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3, "er_annual": 0.05},
}


def _snapshot(regime: MarketRegime) -> MarketRegimeSnapshot:
    return MarketRegimeSnapshot(
        asof=_ASOF,
        benchmark_ticker="1321",
        eval_date=_ASOF,
        benchmark_return_20d=0.05,
        benchmark_return_60d=0.08,
        benchmark_gap_from_high=-0.02,
        regime=regime,
    )


class SelectionMarketStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)
        self.candidates = (
            candidate_record_from_mapping(_DECLINER_CANDIDATE),
            candidate_record_from_mapping(_CALM_CANDIDATE),
        )

    def _payload(self, market_regime: MarketRegimeSnapshot | None) -> dict[str, object]:
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=market_regime,
        )

    def _recommended_tickers(self, payload: dict[str, object]) -> list[str]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return [item["ticker"] for item in recommendations]

    def _diagnostics(self, payload: dict[str, object]) -> Mapping[str, object]:
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        diagnostics = selection["diagnostics"]
        assert isinstance(diagnostics, Mapping)
        return diagnostics

    def _recommendations(self, payload: dict[str, object]) -> list[Mapping[str, object]]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return recommendations

    def test_ranking_follows_er_not_price_decline(self) -> None:
        # 直近の急落は順位を押し上げない: E[r] が高い calm value が
        # sales-discount-growth の decliner より先に並ぶ。
        payload = self._payload(None)
        self.assertEqual(self._recommended_tickers(payload), ["1111", "9999"])

    def test_ranking_is_invariant_across_market_regimes(self) -> None:
        # market regime は fact annotation であり、ranking を変えない。
        expected = ["1111", "9999"]
        for regime in (
            MarketRegime.NEUTRAL_RANGE,
            MarketRegime.RISK_ON_RALLY,
            MarketRegime.RISK_OFF_SELLOFF,
        ):
            payload = self._payload(_snapshot(regime))
            self.assertEqual(self._recommended_tickers(payload), expected, regime)

    def test_diagnostics_record_market_regime_fact(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.RISK_ON_RALLY))
        diagnostics = self._diagnostics(payload)
        market_regime = diagnostics["market_regime"]
        assert isinstance(market_regime, Mapping)
        self.assertEqual(market_regime["regime"], "risk_on_rally")

    def test_diagnostics_market_regime_degrades_to_none(self) -> None:
        payload = self._payload(None)
        self.assertIsNone(self._diagnostics(payload)["market_regime"])

    def test_snapshot_supplies_benchmark_relative_20d_and_laggard_tag(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.NEUTRAL_RANGE))
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        decliner = by_ticker["9999"]
        self.assertAlmostEqual(float(str(decliner["benchmark_relative_20d"])), -0.17)
        self.assertIn("benchmark_laggard_20d", decliner["risk_tags"])
        calm = by_ticker["1111"]
        self.assertAlmostEqual(float(str(calm["benchmark_relative_20d"])), -0.03)
        self.assertIn("benchmark_laggard_20d", calm["risk_tags"])

    def test_without_snapshot_benchmark_relative_20d_degrades_to_none(self) -> None:
        payload = self._payload(None)
        for item in self._recommendations(payload):
            self.assertIsNone(item["benchmark_relative_20d"])
            self.assertNotIn("benchmark_laggard_20d", item["risk_tags"])

    def test_durability_rating_is_annotated(self) -> None:
        payload = self._payload(None)
        diagnostics = self._diagnostics(payload)
        durability_counts = diagnostics["durability_counts"]
        assert isinstance(durability_counts, Mapping)
        self.assertEqual(sum(durability_counts.values()), 2)
        for item in self._recommendations(payload):
            self.assertIn("durability_rating", item)

    def test_price_history_gap_tag_marks_old_listing_with_sparse_bars(self) -> None:
        gappy = dict(_CALM_CANDIDATE)
        gappy["listing_span_days"] = 1200
        gappy["price_history_coverage_750d"] = 0.27
        fresh = dict(_DECLINER_CANDIDATE)
        fresh["listing_span_days"] = 300
        fresh["price_history_coverage_750d"] = 0.27
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(
                candidate_record_from_mapping(gappy),
                candidate_record_from_mapping(fresh),
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
        )
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        self.assertIn("price_history_gap", by_ticker["1111"]["risk_tags"])
        # A genuinely new listing is short_history territory, not a gap.
        self.assertNotIn("price_history_gap", by_ticker["9999"]["risk_tags"])

    def test_an_unmeasurable_deterioration_gate_becomes_a_risk_tag(self) -> None:
        """Passing the gate is not the same as not deteriorating.

        The gate answers "did any observed YoY fall past the threshold", so a name with
        all three missing passes with no evidence either way. That is 12.4% of the
        population and 7.1% of screen passers, and nothing on the decision surface told
        them apart from names the gate actually cleared.
        """
        blind = dict(_CALM_CANDIDATE)
        blind["metrics"] = {
            **dict(_CALM_CANDIDATE["metrics"]),  # type: ignore[arg-type]
            "deterioration_gate_unmeasurable": True,
        }
        seeing = dict(_DECLINER_CANDIDATE)
        seeing["metrics"] = {
            **dict(_DECLINER_CANDIDATE["metrics"]),  # type: ignore[arg-type]
            "deterioration_gate_unmeasurable": False,
        }
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(
                candidate_record_from_mapping(blind),
                candidate_record_from_mapping(seeing),
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
        )
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        self.assertIn("deterioration_unmeasurable", by_ticker["1111"]["risk_tags"])
        self.assertNotIn("deterioration_unmeasurable", by_ticker["9999"]["risk_tags"])

    def test_a_scheduled_earnings_date_becomes_a_risk_tag(self) -> None:
        # 決算日が判明している銘柄は、深掘りの前に event risk を判断面へ出す。
        # 日付が無い銘柄と同じ tag を付けると「予定が無い」と区別できなくなる。
        scheduled = {**_CALM_CANDIDATE, "next_earnings_date": "2026-08-06"}
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(
                candidate_record_from_mapping(scheduled),
                candidate_record_from_mapping(_DECLINER_CANDIDATE),
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
        )

        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}

        self.assertIn("earnings_scheduled", by_ticker["1111"]["risk_tags"])
        self.assertNotIn("earnings_scheduled", by_ticker["9999"]["risk_tags"])

    def test_split_adjustment_flag_becomes_risk_tag(self) -> None:
        # 分割・併合直後は market_cap / net_cash 比率が corporate action 未反映で
        # 歪み得るため、triage 段階で risk tag として必ず表面化させる。
        split_hit = dict(_CALM_CANDIDATE)
        split_hit["split_adjustment_flag"] = True
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(candidate_record_from_mapping(split_hit),),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
        )
        item = self._recommendations(payload)[0]
        self.assertTrue(item["split_adjustment_flag"])
        self.assertIn("split_adjustment_recent", item["risk_tags"])

    def test_forecast_special_gain_flag_becomes_risk_tag(self) -> None:
        # 会社予想の純利益>経常は特別益をほぼ確定する。forward PER / 予想配当 / E[r]
        # carry が一時益で嵩上げされた value trap を triage で表面化させ、event_warnings
        # にも写す (rank は変えない warning 境界)。
        gain_hit = dict(_CALM_CANDIDATE)
        gain_hit["metrics"] = {
            "ocf_yield": 0.12,
            "net_cash_to_market_cap": 0.3,
            "er_annual": 0.05,
            "forecast_special_gain_flag": True,
        }
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(candidate_record_from_mapping(gain_hit),),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
            longlist_top=10,
        )
        item = self._recommendations(payload)[0]
        self.assertIn("forecast_special_gain", item["risk_tags"])
        longlist = payload["longlist"]
        assert isinstance(longlist, list)
        self.assertIn("forecast_special_gain", longlist[0]["event_warnings"])

    def test_forecast_full_year_loss_flag_becomes_risk_tag(self) -> None:
        # 会社自身の通期赤字予想は forward PER を落として FV アンカーを自己履歴 PBR だけに
        # する。事実を triage と event_warnings に出し、rank と E[r] は変えない。
        loss_hit = dict(_CALM_CANDIDATE)
        loss_hit["metrics"] = {
            "ocf_yield": 0.12,
            "net_cash_to_market_cap": 0.3,
            "er_annual": 0.05,
            "forecast_full_year_loss_flag": True,
        }
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(candidate_record_from_mapping(loss_hit),),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
            longlist_top=10,
        )
        item = self._recommendations(payload)[0]
        self.assertIn("forecast_full_year_loss", item["risk_tags"])
        longlist = payload["longlist"]
        assert isinstance(longlist, list)
        self.assertIn("forecast_full_year_loss", longlist[0]["event_warnings"])

    def test_sweep_payload_records_market_regime(self) -> None:
        payload = build_selection_sweep_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profiles=("balanced",),
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=_snapshot(MarketRegime.RISK_ON_RALLY),
        )
        market_regime = payload["market_regime"]
        assert isinstance(market_regime, Mapping)
        self.assertEqual(market_regime["regime"], "risk_on_rally")

    def test_all_detail_modes_expose_decision_input_seed_without_candidate_ref(self) -> None:
        for detail in ("summary", "full"):
            with self.subTest(detail=detail):
                payload = build_selection_payload(
                    asof_date=_ASOF,
                    candidates=self.candidates,
                    macro_context=None,
                    rules=self.rules,
                    top=10,
                    profile="balanced",
                    candidates_ref="local-candidates.yaml",
                    macro_context_ref=None,
                    market_regime=None,
                    detail=detail,
                )

                seed = self._recommendations(payload)[0]["decision_input_seed"]
                assert isinstance(seed, Mapping)
                self.assertEqual(seed["snapshot_version"], 1)
                self.assertEqual(seed["producer_model_version"], "screening-selection-v1")
                self.assertEqual(seed["as_of"], _ASOF.isoformat())
                self.assertIn(seed["completeness"], {"ready_for_enrichment", "missing_valuation"})
                self.assertIn("market_price", seed["required_enrichment"])
                self.assertIsInstance(seed["estimates"], Mapping)
                estimates = seed["estimates"]
                assert isinstance(estimates, Mapping)
                fair_value = estimates["fair_value"]
                assert isinstance(fair_value, Mapping)
                self.assertEqual(fair_value["unit"], "JPY_per_share")
                self.assertIn("model_version", fair_value)
                self.assertIn("assumptions", fair_value)
                self.assertNotIn("candidate_ref", seed)

    def test_longlist_copies_the_decision_seed_estimates(self) -> None:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="local-candidates.yaml",
            macro_context_ref=None,
            market_regime=None,
            longlist_top=2,
        )
        recommendations = {item["ticker"]: item for item in self._recommendations(payload)}
        longlist = payload["longlist"]
        assert isinstance(longlist, list)
        for row in longlist:
            snapshot = row["estimate_snapshot"]
            assert isinstance(snapshot, Mapping)
            self.assertEqual(snapshot["as_of"], _ASOF.isoformat())
            self.assertEqual(
                {key: value for key, value in snapshot.items() if key != "as_of"},
                recommendations[row["ticker"]]["decision_input_seed"]["estimates"],
            )


class MarketStateCliArgumentTests(unittest.TestCase):
    def test_select_parser_has_sqlite_path_default(self) -> None:
        args = build_parser().parse_args(
            ["select", "--asof", "2026-05-29", "--run-revision-id", "run-test"]
        )
        self.assertTrue(args.sqlite_path.endswith("market.sqlite"))

    def test_select_parser_accepts_sqlite_path(self) -> None:
        args = build_parser().parse_args(
            [
                "select",
                "--asof",
                "2026-05-29",
                "--run-revision-id",
                "run-test",
                "--sqlite-path",
                "x.sqlite",
            ]
        )
        self.assertEqual(args.sqlite_path, "x.sqlite")

    def test_select_rules_path_default_honors_env(self) -> None:
        # docs/reference/screening-runtime.md §3: SCREENING_RULES_PATH は select にも効く。
        # default は build_parser() 呼び出し時に env 解決される。CLI 明示 > env > 既定。
        import os
        import unittest.mock

        with unittest.mock.patch.dict(os.environ, {"SCREENING_RULES_PATH": "/tmp/env-rules.yaml"}):
            args = build_parser().parse_args(
                ["select", "--asof", "2026-05-29", "--run-revision-id", "run-test"]
            )
            self.assertEqual(args.rules_path, "/tmp/env-rules.yaml")
            explicit = build_parser().parse_args(
                [
                    "select",
                    "--asof",
                    "2026-05-29",
                    "--run-revision-id",
                    "run-test",
                    "--rules-path",
                    "cli.yaml",
                ]
            )
            self.assertEqual(explicit.rules_path, "cli.yaml")


if __name__ == "__main__":
    unittest.main()
