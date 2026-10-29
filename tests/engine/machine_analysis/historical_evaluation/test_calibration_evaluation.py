from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr
from dataclasses import replace
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from tests.helpers.calibration_store import (
    forward_row,
    panel_diagnostics,
    panel_row,
)

ROOT = Path(__file__).resolve().parents[4]

from baibai_engine.cli.historical_calibration import _required_metric_statuses
from baibai_engine.cli.historical_calibration import (
    calibration_evaluate_command as _calibration_evaluate_command,
)
from baibai_engine.machine_analysis.historical_evaluation.context.builder import (
    CalibrationContextError,
    build_er_distribution_context,
)
from baibai_engine.machine_analysis.historical_evaluation.context.contracts import (
    ErCalibrationContextArtifact,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation import MIN_AXIS_SAMPLE
from baibai_engine.machine_analysis.historical_evaluation.evaluation import (
    evaluate_cohorts as _evaluate_cohorts,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.axes import (
    _half_split_effect,
    _stratified_quality_control,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.discovery import (
    _evaluate_candidate_discovery,
    _observation_dependence,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.sensitivity import (
    _margin_short_to_adv_adoption_sign,
    _metric_direction_stability,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.statistics import _spearman
from baibai_engine.machine_analysis.historical_evaluation.evidence import (
    CANDIDATE_DISCOVERY_APPROACH_SUBJECT,
    CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    ESTIMATOR_POLICY_MANDATORY_METRICS,
    KNOWN_METRICS,
    candidate_discovery_approach_fidelity_metric,
)
from baibai_engine.machine_analysis.historical_evaluation.forward import ForwardReturnRow
from baibai_engine.machine_analysis.historical_evaluation.horizons import require_horizon
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    ScreeningRules,
    load_screening_rules,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rules_identity import (
    production_rules_contract_hash,
)
from baibai_engine.machine_analysis.historical_evaluation.panel import (
    PRODUCTION_PANEL_POLICY,
    PanelDiagnostics,
    PanelRow,
    rules_content_hash,
)
from baibai_engine.machine_analysis.historical_evaluation.store import write_forward, write_panel

_ASOF = "2025-06-30"
_RULES = load_screening_rules()
_RULES_HASH = rules_content_hash(_RULES, PRODUCTION_PANEL_POLICY)


def evaluate_cohorts(
    panels: object,
    forwards: object,
    *,
    horizons: list[str],
    rules: ScreeningRules = _RULES,
    candidate_discovery_input_statuses: dict[str, str] | None = None,
) -> dict[str, object]:
    return _evaluate_cohorts(  # type: ignore[arg-type]
        panels,
        forwards,
        horizons=horizons,
        candidate_discovery_rules=rules.candidate_discovery,
        candidate_discovery_input_statuses=candidate_discovery_input_statuses,
    )


def calibration_evaluate_command(*, rules: ScreeningRules = _RULES, **kwargs: object) -> int:
    return _calibration_evaluate_command(rules=rules, **kwargs)  # type: ignore[arg-type]


def _panel_row(ticker: str, *, rank: int | None = None, **overrides: object) -> PanelRow:
    """A row for this file's one cohort: eligible under common criteria, dated `_ASOF`.

    Everything else comes from `tests.helpers.calibration_store.panel_row`, so a new
    panel field lands there once instead of in a second copy of the row shape.
    """

    if rank is not None:
        # This file's cohort is one where every ranked name also passed, which is what
        # its evaluation cases are about; the panel does not require that in general.
        overrides.setdefault("in_nomination_union", True)
    context: dict[str, object] = {
        "market_cap_oku": 500.0,
        "avg_turnover_oku": 5.0,
        "price_history_span_days": 1200,
        "close": 1000.0,
        "price_history_coverage_750d": 1.0,
    }
    return panel_row(_ASOF, ticker, **(context | overrides))


def _forward_row(
    ticker: str,
    price_return: float,
    *,
    total_return: float | None = None,
    horizon: str = "6m",
) -> ForwardReturnRow:
    """One resolved forward observation for this file's cohort and requested horizon."""

    target = require_horizon(horizon).target_date(date.fromisoformat(_ASOF)).isoformat()
    return forward_row(
        _ASOF,
        ticker,
        horizon,
        target_date=target,
        price_return=price_return,
        entry_date=_ASOF,
        exit_date=target,
        total_return=total_return,
        total_return_status=(
            "resolved" if total_return is not None else "unresolved_missing_dividend"
        ),
    )


def _panel_diagnostics(
    *,
    master_snapshot_status: str = "unavailable",
    asof_population_mismatch_count: int = 0,
    priced_master_without_universe_count: int = 0,
) -> PanelDiagnostics:
    """This file's cohort: 240 names, all with an exact trailing multiple."""

    return panel_diagnostics(
        _ASOF,
        rules_hash=_RULES_HASH,
        universe_size=240,
        population_size=240,
        effective_bars_start="2024-01-01",
        effective_fin_start="2024-01-01",
        population_per_trailing_nonnull=240,
        population_per_trailing_exact=240,
        master_snapshot_status=master_snapshot_status,
        asof_population_mismatch_count=asof_population_mismatch_count,
        priced_master_without_universe_count=priced_master_without_universe_count,
    )


class SpearmanTest(unittest.TestCase):
    def test_spearman_perfect_monotone_is_one(self) -> None:
        pairs = [(float(i), float(i * 2)) for i in range(40)]
        ic = _spearman(pairs)
        assert ic is not None
        self.assertAlmostEqual(ic, 1.0)

    def test_spearman_perfect_inverse_is_minus_one(self) -> None:
        pairs = [(float(i), float(-i)) for i in range(40)]
        ic = _spearman(pairs)
        assert ic is not None
        self.assertAlmostEqual(ic, -1.0)

    def test_spearman_below_min_sample_is_none(self) -> None:
        self.assertIsNone(_spearman([(1.0, 1.0)] * 10))


class RequiredMetricStatusTest(unittest.TestCase):
    def test_missing_non_mapping_and_unknown_statuses_fail_closed(self) -> None:
        required = ("er_calibration", "er_level_calibration")
        self.assertEqual(
            _required_metric_statuses({"er_calibration": "eligible"}, required),
            {"er_calibration": "eligible", "er_level_calibration": "unresolved"},
        )
        self.assertEqual(
            _required_metric_statuses(None, required),
            {"er_calibration": "unresolved", "er_level_calibration": "unresolved"},
        )
        self.assertEqual(
            _required_metric_statuses(
                {"er_calibration": "eligible", "er_level_calibration": "claimed"}, required
            ),
            {"er_calibration": "eligible", "er_level_calibration": "unresolved"},
        )


class DeteriorationGateTest(unittest.TestCase):
    """The gate is judged inside the best decile of the axis it guards."""

    @staticmethod
    def _cohort(blocked_rows: int) -> dict[str, object]:
        # 400 names so the best decile holds 40; the deteriorating ones sit inside it.
        panel = []
        forwards = []
        for index in range(400):
            deteriorating = index < blocked_rows
            row = _panel_row(
                f"{4000 + index}",
                # Lower per_trailing is the better side, so the low indices are the
                # best decile and the deteriorating rows land there.
                per_trailing=1.0 + index / 100,
                operating_profit_yoy=-0.5 if deteriorating else 0.1,
            )
            panel.append(row)
            forwards.append(_forward_row(row.ticker, 0.30 if deteriorating else 0.05))
        return evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])[
            "6m"
        ]

    def test_a_populated_blocked_side_reports_the_gate_effect(self) -> None:
        aggregate = self._cohort(blocked_rows=25)["aggregate"]
        assert isinstance(aggregate, dict)
        entry = aggregate["gates"]["per_trailing"]
        assert isinstance(entry, dict)
        self.assertEqual(entry["eligible_cohorts"], 1)
        assert isinstance(entry["mean_gate_median_excess_delta"], float)
        # The names the gate removed did better, so the gate cost return here.
        self.assertLess(entry["mean_gate_median_excess_delta"], 0.0)

    def test_a_thin_blocked_side_is_counted_and_left_out_of_the_mean(self) -> None:
        """The blocked side is the deteriorating names inside one decile, so it is small.

        Averaging a median over a handful of them reads like the same evidence as a
        gate measured on hundreds.
        """
        aggregate = self._cohort(blocked_rows=5)["aggregate"]
        assert isinstance(aggregate, dict)
        entry = aggregate["gates"]["per_trailing"]
        assert isinstance(entry, dict)
        self.assertEqual(entry["cohorts"], 1)
        self.assertEqual(entry["eligible_cohorts"], 0)
        self.assertIsNone(entry["mean_gate_median_excess_delta"])
        self.assertIsNone(entry["gate_positive_share"])
        self.assertEqual(entry["blocked_n"], 5)


class SectorMedianBasisThinSideTest(unittest.TestCase):
    """The market side is a fraction of the cohort, so it has to report without deciles.

    Only nine sectors sit below the head-count floor, which leaves roughly fifty rows a
    cohort on the market baseline against several thousand on their own. A decile over
    fifty rows puts five names in a bucket, so the axis minimum refuses the spread — and
    the group statistics have to carry the comparison, or the thin side reports nothing at
    all and the coordinate cannot answer the question it exists for.
    """

    def _cohort(self) -> dict[str, object]:
        panel = []
        forwards = []
        for index in range(150):
            row = _panel_row(f"{4000 + index}", per_trailing=10.0, smg_p_s=-index / 100)
            panel.append(row)
            forwards.append(_forward_row(row.ticker, 0.05))
        # Below MIN_AXIS_SAMPLE on purpose: this is the real shape of the market side.
        for index in range(40):
            row = _panel_row(
                f"{5000 + index}",
                per_trailing=10.0,
                smg_p_s=-index / 100,
                smg_market_fallback="p_s",
                in_nomination_union=True,
            )
            panel.append(row)
            # The group sits well above the population and the axis orders it inside:
            # the level is what the group did, the ordering is what the axis is worth.
            forwards.append(_forward_row(row.ticker, 0.30 + index / 200))
        return evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])[
            "6m"
        ]

    def test_the_thin_side_reports_its_group_without_a_spread(self) -> None:
        cohort = self._cohort()["cohorts"][0]
        assert isinstance(cohort, dict)
        node = cohort["sector_median_basis"]
        assert isinstance(node, dict)
        axis = node["smg_p_s"]
        assert isinstance(axis, dict)
        market = axis["market_fallback"]
        assert isinstance(market, dict)
        self.assertEqual(market["n"], 40)
        self.assertEqual(market["passed_screen"], 40)
        # Too few rows for a decile, so the spread stays absent rather than being made up.
        self.assertIsNone(market["decile_spread_median"])
        # The group's own level is reported under a name that says so.
        assert isinstance(market["group_median_excess"], float)
        self.assertGreater(market["group_median_excess"], 0.0)
        # The half split needs far less sample, so the axis is measured on this side too.
        effect = market["axis_effect"]
        assert isinstance(effect, dict)
        self.assertEqual(effect["n"], 40)
        assert isinstance(effect["median_excess_delta"], float)
        self.assertAlmostEqual(effect["median_excess_delta"], 0.10, places=6)

    def test_a_group_the_axis_does_not_order_reports_a_level_and_no_effect(self) -> None:
        """A level the group carries is not an effect the axis produced.

        Falling back is decided per sector, so the market side is whole sectors and its
        level may reflect that mix. This fixture gives every fallback row the same
        return, so its within-group split is zero however far the group sits from the
        population. The split does not control sector composition or identify causality.
        """
        panel = []
        forwards = []
        for index in range(120):
            row = _panel_row(f"{4000 + index}", per_trailing=10.0, smg_p_s=-index / 100)
            panel.append(row)
            forwards.append(_forward_row(row.ticker, 0.05))
        for index in range(40):
            row = _panel_row(
                f"{5000 + index}",
                per_trailing=10.0,
                smg_p_s=-index / 100,
                smg_market_fallback="p_s",
            )
            panel.append(row)
            forwards.append(_forward_row(row.ticker, 0.30))
        cohort = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])[
            "6m"
        ]["cohorts"][0]
        assert isinstance(cohort, dict)
        node = cohort["sector_median_basis"]
        assert isinstance(node, dict)
        axis = node["smg_p_s"]
        assert isinstance(axis, dict)
        market = axis["market_fallback"]
        assert isinstance(market, dict)
        assert isinstance(market["group_median_excess"], float)
        self.assertGreater(market["group_median_excess"], 0.2)
        effect = market["axis_effect"]
        assert isinstance(effect, dict)
        self.assertEqual(effect["median_excess_delta"], 0.0)

    def test_the_aggregate_counts_the_thin_side_as_a_cohort(self) -> None:
        aggregate = self._cohort()["aggregate"]
        assert isinstance(aggregate, dict)
        node = aggregate["sector_median_basis"]
        assert isinstance(node, dict)
        axis = node["smg_p_s"]
        assert isinstance(axis, dict)
        market = axis["market_fallback"]
        assert isinstance(market, dict)
        # It contributed a cohort and an axis effect even though it contributed no spread.
        self.assertEqual(market["cohorts"], 1)
        self.assertEqual(market["spread_cohorts"], 0)
        self.assertIsNone(market["mean_decile_spread_median"])
        self.assertEqual(market["effect_cohorts"], 1)
        self.assertEqual(market["axis_effect_positive_share"], 1.0)
        assert isinstance(market["mean_axis_effect"], float)
        self.assertAlmostEqual(market["mean_axis_effect"], 0.10, places=6)


class SectorMedianBasisTest(unittest.TestCase):
    """The sector-gap axes are reported separately for each baseline that produced them."""

    def _cohort(self) -> dict[str, object]:
        # Two groups whose gaps are drawn from the same numbers but whose outcomes run
        # opposite ways, so a single pooled result would report roughly nothing and only
        # the split can show either effect.
        panel = []
        forwards = []
        for index in range(120):
            own = _panel_row(f"{4000 + index}", per_trailing=10.0, smg_p_s=-index / 100)
            market = _panel_row(
                f"{5000 + index}",
                per_trailing=10.0,
                smg_p_s=-index / 100,
                smg_market_fallback="p_s",
                in_nomination_union=True,
            )
            panel.extend((own, market))
            # own_sector: the cheapest end wins. market_fallback: it loses.
            forwards.append(_forward_row(own.ticker, index / 200))
            forwards.append(_forward_row(market.ticker, -index / 200))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        return result["6m"]

    def test_each_baseline_reports_its_own_effect(self) -> None:
        cohort = self._cohort()["cohorts"][0]
        assert isinstance(cohort, dict)
        node = cohort["sector_median_basis"]
        assert isinstance(node, dict)
        axis = node["smg_p_s"]
        assert isinstance(axis, dict)
        own, market = axis["own_sector"], axis["market_fallback"]
        assert isinstance(own, dict)
        assert isinstance(market, dict)
        self.assertEqual(own["n"], 120)
        self.assertEqual(market["n"], 120)
        # smg_p_s has direction -1, so the favoured end is the most negative gap.
        assert isinstance(own["decile_spread_median"], float)
        assert isinstance(market["decile_spread_median"], float)
        self.assertGreater(own["decile_spread_median"], 0.0)
        self.assertLess(market["decile_spread_median"], 0.0)
        # Screen passage is counted per basis, which is what says how far a fallback
        # reaches into the output.
        self.assertEqual(own["passed_screen"], 0)
        self.assertEqual(market["passed_screen"], 120)

    def test_the_aggregate_keeps_the_two_baselines_apart(self) -> None:
        aggregate = self._cohort()["aggregate"]
        assert isinstance(aggregate, dict)
        node = aggregate["sector_median_basis"]
        assert isinstance(node, dict)
        axis = node["smg_p_s"]
        assert isinstance(axis, dict)
        own, market = axis["own_sector"], axis["market_fallback"]
        assert isinstance(own, dict)
        assert isinstance(market, dict)
        self.assertEqual(own["cohorts"], 1)
        self.assertEqual(own["decile_spread_positive_share"], 1.0)
        self.assertEqual(market["decile_spread_positive_share"], 0.0)
        self.assertEqual(market["passed_screen"], 120)


class EvaluateCohortsTest(unittest.TestCase):
    @staticmethod
    def _rules_with_alternate_common_eligibility() -> ScreeningRules:
        eligibility = _RULES.candidate_discovery.common_eligibility.model_copy(
            update={"min_market_cap_oku": 101}
        )
        candidate_discovery = _RULES.candidate_discovery.model_copy(
            update={"common_eligibility": eligibility}
        )
        return _RULES.model_copy(update={"candidate_discovery": candidate_discovery})

    @staticmethod
    def _rules_with_alternate_approach_method() -> ScreeningRules:
        approaches = dict(_RULES.candidate_discovery.approaches)
        current = approaches["current-earnings-power"]
        approaches["current-earnings-power"] = current.model_copy(
            update={"method_id": "current-earnings-power-v2"}
        )
        candidate_discovery = _RULES.candidate_discovery.model_copy(
            update={"approaches": approaches}
        )
        return _RULES.model_copy(update={"candidate_discovery": candidate_discovery})

    def test_approach_rank_parser_uses_candidate_discovery_nomination_depth(self) -> None:
        candidate_discovery = _RULES.candidate_discovery.model_copy(update={"nomination_depth": 3})
        rules = _RULES.model_copy(update={"candidate_discovery": candidate_discovery})
        panel = [
            _panel_row(
                f"2{index:03d}",
                rank=index + 1,
                valuation_approach_ranks="current-earnings-power:4",
            )
            for index in range(120)
        ]
        forwards = [_forward_row(row.ticker, 0.1) for row in panel]

        result = evaluate_cohorts({_ASOF: panel}, {_ASOF: forwards}, horizons=["6m"], rules=rules)[
            "6m"
        ]

        assert isinstance(result, dict)
        cohort = result["cohorts"][0]
        assert isinstance(cohort, dict)
        current_top20 = cohort["candidate_discovery"]["approach_current_earnings_power_top20"]
        self.assertEqual(current_top20["n"], 0)

    def test_nomination_outcome_uses_configured_depth_not_fixed_top20(self) -> None:
        rules = _RULES.candidate_discovery.model_copy(update={"nomination_depth": 25})
        panel = [
            _panel_row(
                f"2{index:03d}",
                rank=index + 1,
                valuation_approach_ranks=f"current-earnings-power:{index + 1}",
            )
            for index in range(25)
        ]
        excess = {row.ticker: (0.0 if index < 20 else 1.0) for index, row in enumerate(panel)}
        result = _evaluate_candidate_discovery(panel, excess, candidate_discovery_rules=rules)
        complete = result["approach_current_earnings_power_nominations"]
        diagnostic = result["approach_current_earnings_power_top20"]
        assert isinstance(complete, dict)
        assert isinstance(diagnostic, dict)
        self.assertEqual(complete["n"], 25)
        self.assertEqual(complete["mean_excess"], 0.2)
        self.assertEqual(diagnostic["n"], 20)
        self.assertEqual(diagnostic["mean_excess"], 0.0)

    def test_full_candidate_discovery_fidelity_requires_complete_input_and_outcomes(
        self,
    ) -> None:
        panel = []
        for index in range(120):
            ranked = index < 20
            panel.append(
                _panel_row(
                    f"3{index:03d}",
                    rank=index + 1 if ranked else None,
                    **(
                        {
                            "valuation_approach_ranks": "|".join(
                                f"{approach}:{index + 1}"
                                for approach in _RULES.candidate_discovery.approaches
                            )
                        }
                        if ranked
                        else {}
                    ),
                )
            )
        forwards = [_forward_row(row.ticker, 0.1) for row in panel]

        cohort = evaluate_cohorts(
            {_ASOF: panel},
            {_ASOF: forwards},
            horizons=["6m"],
            candidate_discovery_input_statuses={_ASOF: "complete"},
        )["6m"]["cohorts"][0]

        assert isinstance(cohort, dict)
        fidelity = cohort["candidate_discovery_fidelity"]
        assert isinstance(fidelity, dict)
        self.assertTrue(fidelity["nomination_union_eligible"])
        statuses = cohort["metric_statuses"]
        assert isinstance(statuses, dict)
        self.assertEqual(statuses[CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC], "eligible")
        for approach in _RULES.candidate_discovery.approaches:
            self.assertEqual(
                statuses[candidate_discovery_approach_fidelity_metric(approach)],
                "eligible",
            )

        missing_input = evaluate_cohorts(
            {_ASOF: panel},
            {_ASOF: forwards},
            horizons=["6m"],
        )["6m"]["cohorts"][0]
        assert isinstance(missing_input, dict)
        missing_statuses = missing_input["metric_statuses"]
        assert isinstance(missing_statuses, dict)
        self.assertEqual(
            missing_statuses[CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC],
            "unresolved",
        )

    def test_approach_fidelity_rejects_one_unresolved_nomination(self) -> None:
        panel = []
        for index in range(120):
            ranked = index < 20
            panel.append(
                _panel_row(
                    f"4{index:03d}",
                    rank=index + 1 if ranked else None,
                    **({"valuation_approach_ranks": f"asset-value:{index + 1}"} if ranked else {}),
                )
            )
        forwards = [_forward_row(row.ticker, 0.1) for index, row in enumerate(panel) if index != 19]

        cohort = evaluate_cohorts(
            {_ASOF: panel},
            {_ASOF: forwards},
            horizons=["6m"],
            candidate_discovery_input_statuses={_ASOF: "complete"},
        )["6m"]["cohorts"][0]

        assert isinstance(cohort, dict)
        statuses = cohort["metric_statuses"]
        assert isinstance(statuses, dict)
        self.assertEqual(
            statuses[candidate_discovery_approach_fidelity_metric("asset-value")],
            "unresolved",
        )

    def test_observation_dependence_reports_entry_years_and_nonoverlap(self) -> None:
        dependence = _observation_dependence(
            [
                {"asof": "2020-01-31", "metric_calculation_status": "resolved"},
                {"asof": "2020-02-29", "metric_calculation_status": "resolved"},
                {"asof": "2021-01-31", "metric_calculation_status": "resolved"},
                {"asof": "2021-02-28", "metric_calculation_status": "unresolved"},
            ],
            horizon="1y",
        )

        self.assertEqual(dependence["resolved_cohort_count"], 3)
        self.assertEqual(dependence["entry_year_counts"], {"2020": 2, "2021": 1})
        self.assertEqual(dependence["greedy_non_overlapping_window_count"], 2)

    def test_calibration_evaluate_rejects_any_method_mismatch_before_writing_metrics(self) -> None:
        panel = [_panel_row("1111", per_trailing=10.0)]
        forwards = [_forward_row("1111", 0.1)]
        cases = {
            "common eligibility": self._rules_with_alternate_common_eligibility(),
            "approach method": self._rules_with_alternate_approach_method(),
        }
        for label, rules in cases.items():
            with self.subTest(label=label):
                errors = StringIO()
                with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
                    root = Path(temp_dir)
                    write_panel(root, date(2025, 6, 30), panel, _panel_diagnostics())
                    write_forward(root, date(2025, 6, 30), forwards)
                    output_path = root / "evaluation.yaml"

                    exit_code = calibration_evaluate_command(
                        calibration_dir=root,
                        rules=rules,
                        horizons=["6m"],
                        output_path=output_path,
                        stdout=StringIO(),
                    )

                    self.assertEqual(exit_code, 1)
                    self.assertFalse(output_path.exists())
                self.assertIn("rules_hash does not match --rules-path", errors.getvalue())

    def test_calibration_evaluate_refuses_pre_dividend_revision_panel_without_relabeling(
        self,
    ) -> None:
        with patch(
            "baibai_engine.machine_analysis.historical_evaluation.panel.VALUATION_CALCULATION_REVISION",
            "same-profit-basis-yoy-v25",
        ):
            old_hash = rules_content_hash(_RULES, PRODUCTION_PANEL_POLICY)
        self.assertNotEqual(old_hash, _RULES_HASH)
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            root = Path(temp_dir)
            write_panel(
                root,
                date(2025, 6, 30),
                [_panel_row("1111")],
                replace(_panel_diagnostics(), rules_hash=old_hash),
            )
            write_forward(root, date(2025, 6, 30), [_forward_row("1111", 0.1)])
            original = (root / "current.sqlite").read_bytes()
            output = root / "evaluation.yaml"
            code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                output_path=output,
                stdout=StringIO(),
            )

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
            self.assertEqual((root / "current.sqlite").read_bytes(), original)
        self.assertIn("rules_hash does not match --rules-path", errors.getvalue())

    def test_calibration_evaluate_rejects_unknown_panel_variant(self) -> None:
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            root = Path(temp_dir)
            diagnostics = replace(_panel_diagnostics(), panel_variant="unknown")  # type: ignore[arg-type]
            write_panel(root, date(2025, 6, 30), [_panel_row("1111")], diagnostics)
            write_forward(root, date(2025, 6, 30), [_forward_row("1111", 0.1)])

            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                stdout=StringIO(),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("unknown panel variant", errors.getvalue())

    def test_calibration_evaluate_rejects_unknown_candidate_input_status(self) -> None:
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            root = Path(temp_dir)
            write_panel(root, date(2025, 6, 30), [_panel_row("1111")], _panel_diagnostics())
            write_forward(root, date(2025, 6, 30), [_forward_row("1111", 0.1)])
            with closing(sqlite3.connect(root / "current.sqlite")) as connection, connection:
                raw = connection.execute("SELECT diagnostics FROM cohort").fetchone()[0]
                diagnostics = json.loads(raw)
                diagnostics["candidate_discovery_jpx_input_status"] = "claimed"
                connection.execute(
                    "UPDATE cohort SET diagnostics = ?",
                    (json.dumps(diagnostics),),
                )

            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                stdout=StringIO(),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("Candidate Discovery input status is invalid", errors.getvalue())

    def test_date_filter_cannot_hide_mixed_rules_in_the_current_snapshot(self) -> None:
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            root = Path(temp_dir)
            write_panel(root, date(2025, 6, 30), [_panel_row("1111")], _panel_diagnostics())
            write_forward(root, date(2025, 6, 30), [_forward_row("1111", 0.1)])
            later = date(2025, 7, 31)
            write_panel(
                root,
                later,
                [replace(_panel_row("2222"), asof=later.isoformat())],
                replace(_panel_diagnostics(), asof=later.isoformat()),
            )
            write_forward(root, later, [])
            with closing(sqlite3.connect(root / "current.sqlite")) as connection, connection:
                raw_diagnostics = connection.execute(
                    "SELECT diagnostics FROM cohort WHERE asof = ?", (later.isoformat(),)
                ).fetchone()[0]
                diagnostics = json.loads(raw_diagnostics)
                diagnostics["rules_hash"] = "different-rules-hash"
                connection.execute(
                    "UPDATE cohort SET diagnostics = ? WHERE asof = ?",
                    (json.dumps(diagnostics), later.isoformat()),
                )

            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                end=date(2025, 6, 30),
                stdout=StringIO(),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("mixes rules provenance", errors.getvalue())

    def test_replays_whose_verdict_was_negative_stay_out_of_the_payload(self) -> None:
        # Each name below has a dated negative verdict. Recomputing one would put a
        # rejected hypothesis back into the artifact the evidence readiness reads.
        panel = [
            replace(
                _panel_row(
                    f"{4000 + index}",
                    per_trailing=10.0 + index,
                    rank=index + 1 if index < 20 else None,
                    er_annual=index / 100,
                    er_reversion_annual=index / 200,
                ),
                margin_std_long_share=0.9 if index < 2 else 0.5,
                margin_long_to_adv=float(index),
            )
            for index in range(120)
        ]
        forwards = [_forward_row(row.ticker, 0.1) for row in panel]

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        assert isinstance(cohort, dict)
        candidate_discovery = cohort["candidate_discovery"]
        assert isinstance(candidate_discovery, dict)

        # An unresolved cohort emits none of these keys either, so the absences
        # below only mean something once the cohort has actually been computed.
        self.assertEqual(cohort["metric_calculation_status"], "resolved")
        for key in ("margin_deadline_gate", "crowded_value"):
            self.assertNotIn(key, cohort)
            self.assertNotIn(key, result["6m"]["aggregate"])
        for key in ("reversion_ranked_top5", "reversion_carry_ranked_top5"):
            self.assertNotIn(key, candidate_discovery)
        self.assertNotIn("margin_deadline_gate_top10", cohort["metric_statuses"])

    def test_new_margin_axes_and_every_registered_control_are_reported(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(150):
            ticker = f"{4000 + index}"
            axis = float(index + 1)
            panel.append(
                replace(
                    _panel_row(
                        ticker,
                        per_trailing=5.0 + index / 10,
                        dividend_yield=index / 1000,
                        sector_33=f"sector-{index % 5}",
                        close=100.0 + index,
                        market_cap_oku=100.0 + index,
                        avg_turnover_oku=1.0 + index / 10,
                        price_change_60d=index / 1000,
                    ),
                    margin_short_to_adv=axis,
                    realized_volatility_60d=0.1 + index / 1000,
                )
            )
            forwards.append(_forward_row(ticker, -index / 200))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        axes = cohort["axes"]
        self.assertGreater(axes["margin_short_to_adv"]["decile_spread_median"], 0)
        self.assertEqual(cohort["metric_statuses"]["margin_short_to_adv"], "eligible")
        hypotheses = cohort["margin_supply_demand_hypotheses"]
        short_controls = hypotheses["margin_short_to_adv"]["controls"]
        self.assertEqual(
            set(short_controls),
            {
                "market_cap_oku",
                "avg_turnover_oku",
                "per_trailing",
                "dividend_yield",
                "close",
                "price_change_60d",
                "realized_volatility_60d",
                "sector_33",
            },
        )
        self.assertNotIn("margin_long_to_adv_mcap_quintile_percentile", hypotheses)

    def test_profit_normalization_reports_controls_and_history_coverage(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(120):
            ticker = f"5{index:03d}"
            panel.append(
                replace(
                    _panel_row(
                        ticker,
                        per_trailing=5.0 + index,
                        pbr=1.0 + index / 100,
                        market_cap_oku=100.0 + index,
                        er_annual=index / 100,
                        er_reversion_annual=index / 200,
                    ),
                    normalized_per_3fy=1.0 + index,
                    normalized_per_5fy=2.0 + index,
                    self_range_observed_sessions=1300,
                )
            )
            forwards.append(_forward_row(ticker, -index / 200))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        hypotheses = cohort["profit_normalization_hypotheses"]
        aggregate = result["6m"]["aggregate"]["profit_normalization_hypotheses"]

        self.assertGreater(cohort["axes"]["normalized_per_3fy"]["decile_spread_median"], 0)
        self.assertEqual(cohort["metric_statuses"]["normalized_per_3fy"], "eligible")
        self.assertEqual(hypotheses["normalized_per_3fy_coverage"], 1.0)
        self.assertEqual(hypotheses["self_range_coverage"]["at_least_750"], 1.0)
        self.assertNotIn("cycle_peak_top_er_decile", aggregate)

    def test_normalized_per_metric_is_unresolved_without_sample(self) -> None:
        panel = [_panel_row(f"M{index:03d}", per_trailing=10.0) for index in range(120)]
        forwards = [_forward_row(row.ticker, 0.0) for row in panel]

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]

        self.assertEqual(cohort["metric_statuses"]["normalized_per_3fy"], "unresolved")

    def test_asset_backed_axis_reports_fixed_interaction_and_controls(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        group_values = (
            (0.5, True, 0.2),
            (0.5, False, 0.0),
            (0.2, True, 0.1),
            (0.2, False, 0.0),
        )
        for group_index, (ratio, return_change, realized) in enumerate(group_values):
            for index in range(25):
                ticker = f"{group_index + 6}{index:03d}"
                panel.append(
                    _panel_row(
                        ticker,
                        per_trailing=10.0 + index / 10,
                        pbr=0.8 + index / 100,
                        market_cap_oku=500.0,
                        net_cash_to_market_cap=ratio - 0.1,
                        investment_securities=5_000_000_000.0,
                        asset_backed_ratio=ratio,
                        equity_ratio=0.5 + index / 1000,
                        shareholder_return_change=return_change,
                    )
                )
                forwards.append(_forward_row(ticker, realized))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]["asset_backed_hypotheses"]
        aggregate = result["6m"]["aggregate"]["asset_backed_hypotheses"]

        self.assertEqual(cohort["asset_backed_ratio_coverage"], 1.0)
        self.assertEqual(set(cohort["controls"]), {"pbr", "market_cap_oku", "equity_ratio"})
        self.assertAlmostEqual(cohort["thick_change_minus_no_change_median_excess"], 0.2)
        self.assertAlmostEqual(cohort["difference_in_differences_median_excess"], 0.1)
        self.assertEqual(aggregate["comparable_cohorts"], 1)
        self.assertEqual(aggregate["difference_in_differences_cohorts"], 1)
        self.assertAlmostEqual(aggregate["mean_difference_in_differences_median_excess"], 0.1)


class MarginSizeNormalizationTest(unittest.TestCase):
    def test_cheap_per_trailing_outperformance_yields_positive_ic(self) -> None:
        # 150 銘柄: PER が低いほど forward return が高い設計 (direction=-1 で
        # 正の IC・best decile 正の超過になるべき) 。上位 10 銘柄を select 順に見立てる。
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        n = 150
        for i in range(n):
            ticker = f"{1000 + i}"
            per = 5.0 + i * 0.5  # 銘柄 i の PER: 安い順
            ret = 0.30 - 0.004 * i  # 安いほど高リターン
            rank = i + 1 if i < 10 else None
            panel.append(_panel_row(ticker, per_trailing=per, rank=rank))
            forwards.append(_forward_row(ticker, ret))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        per_axis = axes["per_trailing"]
        assert isinstance(per_axis, dict)
        rank_ic = per_axis["rank_ic"]
        assert isinstance(rank_ic, float)
        self.assertAlmostEqual(rank_ic, 1.0)
        best_median = per_axis["best_decile_median_excess"]
        assert isinstance(best_median, float)
        self.assertGreater(best_median, 0.0)
        spread = per_axis["decile_spread_median"]
        assert isinstance(spread, float)
        self.assertGreater(spread, 0.0)
        # candidate_discovery replay: 上位 10 銘柄は安い側なので正の超過。
        candidate_discovery = cohorts[0]["candidate_discovery"]
        assert isinstance(candidate_discovery, dict)
        nomination_union = candidate_discovery["nomination_union_all"]
        assert isinstance(nomination_union, dict)
        self.assertEqual(nomination_union["n"], 10)
        median_excess = nomination_union["median_excess"]
        assert isinstance(median_excess, float)
        self.assertGreater(median_excess, 0.0)
        # aggregate 構造: 1 cohort・IC 勝率 1.0。
        aggregate = horizon["aggregate"]
        assert isinstance(aggregate, dict)
        self.assertEqual(aggregate["cohort_count"], 1)

    def test_price_return_only_does_not_accrue_dividend_yield(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"{2000 + i}"
            dy = 0.04 if i == 0 else 0.0
            panel.append(_panel_row(ticker, per_trailing=10.0, dividend_yield=dy))
            forwards.append(_forward_row(ticker, 0.0))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["metric_basis"], "price_return_only")
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        dy_axis = axes["dividend_yield"]
        assert isinstance(dy_axis, dict)
        deciles = dy_axis["deciles"]
        assert isinstance(deciles, list)
        best = deciles[-1]
        assert isinstance(best, dict)
        # 配当利回りだけでは price return diagnostic の excess を作らない。
        mean_excess = best["mean_excess"]
        assert isinstance(mean_excess, float)
        self.assertEqual(mean_excess, 0.0)

    def test_shareholder_return_change_compares_low_per_band_and_controls(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(500):
            ticker = f"{5000 + i}"
            in_low_per_band = i < 100
            change = in_low_per_band and i % 2 == 0
            control = float(i % 20 + 1)
            row = _panel_row(
                ticker,
                per_trailing=float(i + 1),
                dividend_yield=control / 1_000,
                market_cap_oku=control * 100,
                avg_turnover_oku=control,
                pbr=control / 10,
                price_change_60d=control / 100,
            )
            if in_low_per_band:
                row = replace(
                    row,
                    dps_streak_up=change,
                    dps_yoy_latest=0.10 if change else 0.0,
                    dps_guidance_up=change,
                    dividend_initiation=change,
                    share_count_reduction_streak=1 if change else 0,
                    shareholder_return_change=change,
                )
            panel.append(row)
            realized = 0.10 if change else (-0.30 if in_low_per_band else 0.0)
            forwards.append(_forward_row(ticker, realized, horizon="1y"))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])
        horizon = result["1y"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        interaction = cohorts[0]["shareholder_return_change"]
        assert isinstance(interaction, dict)
        self.assertEqual(interaction["low_valuation_n"], 100)
        self.assertEqual(interaction["eligible_n"], 100)
        self.assertEqual(interaction["median_excess_delta"], 0.4)
        self.assertEqual(interaction["trap_rate_delta"], -1.0)
        controls = interaction["controls"]
        assert isinstance(controls, dict)
        for field_name in (
            "dividend_yield",
            "per_trailing",
            "pbr",
            "market_cap_oku",
            "avg_turnover_oku",
            "price_change_60d",
        ):
            with self.subTest(field_name=field_name):
                control_result = controls[field_name]
                assert isinstance(control_result, dict)
                self.assertEqual(control_result["strata_used"], 2)
                self.assertEqual(control_result["matched_weight"], 50)
                self.assertEqual(control_result["stratified_median_excess_delta"], 0.4)
                self.assertEqual(control_result["stratified_trap_rate_delta"], -1.0)
        components = interaction["components"]
        assert isinstance(components, dict)
        for field_name in ("dps_streak_up", "dps_guidance_up", "dividend_initiation"):
            component = components[field_name]
            assert isinstance(component, dict)
            self.assertEqual(component["median_excess_delta"], 0.4)

        aggregate = horizon["aggregate"]
        assert isinstance(aggregate, dict)
        aggregate_interaction = aggregate["shareholder_return_change"]
        assert isinstance(aggregate_interaction, dict)
        self.assertEqual(aggregate_interaction["change_n"], 50)
        self.assertEqual(aggregate_interaction["no_change_n"], 50)
        self.assertEqual(aggregate_interaction["mean_median_excess_delta"], 0.4)

    def test_er_ranked_replay_is_independent_of_nomination_union(self) -> None:
        # E[r] diagnostic uses the population; nomination membership only changes overlap.
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        n = 120
        for i in range(n):
            ticker = f"{3000 + i}"
            row = _panel_row(ticker, per_trailing=10.0, rank=(i + 1 if i < 20 else None))
            # PanelRow は frozen なので必要 field を差し替えた新 instance を作る。
            row = replace(row, er_annual=0.20 - 0.001 * i, in_nomination_union=i < 20)
            panel.append(row)
            forwards.append(_forward_row(ticker, 0.001 * i))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        candidate_discovery = cohorts[0]["candidate_discovery"]
        assert isinstance(candidate_discovery, dict)
        er_top5 = candidate_discovery["pure_er_top5"]
        assert isinstance(er_top5, dict)
        # er 最上位 5 銘柄 = i=0..4 = return 最低群 → 負の excess
        self.assertEqual(er_top5["n"], 5)
        median_excess = er_top5["median_excess"]
        assert isinstance(median_excess, float)
        self.assertLess(median_excess, 0.0)
        changed = evaluate_cohorts(
            {
                _ASOF: [
                    replace(row, in_nomination_union=index >= 100)
                    for index, row in enumerate(panel)
                ]
            },
            {_ASOF: forwards},
            horizons=["6m"],
        )["6m"]
        self.assertEqual(changed["cohorts"][0]["candidate_discovery"]["pure_er_top5"], er_top5)
        self.assertEqual(
            changed["aggregate"]["candidate_discovery"]["nomination_union_vs_er_top5"][
                "mean_overlap_n"
            ],
            0.0,
        )
        aggregate = horizon["aggregate"]["candidate_discovery"]
        self.assertEqual(aggregate["nomination_union_vs_er_top5"]["cohorts"], 1)
        self.assertEqual(aggregate["nomination_union_vs_er_top5"]["mean_overlap_n"], 5.0)
        self.assertEqual(aggregate["nomination_union_vs_er_top5"]["mean_overlap_share"], 1.0)

    def test_er_calibration_compares_centered_reversion_with_centered_price_return(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(100):
            ticker = f"35{i:02d}"
            reversion = i / 1000
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0,
                    er_annual=reversion + 0.03,
                    er_reversion_annual=reversion,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, i / 1000))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])

        calibration = result["6m"]["cohorts"][0]["er_calibration"]
        assert isinstance(calibration, dict)
        self.assertEqual(
            calibration["prediction_basis"],
            "er_reversion_annual_relative_to_population_median",
        )
        self.assertEqual(
            calibration["realized_basis"], "price_return_relative_to_population_median"
        )
        self.assertEqual(calibration["calibration_error_basis"], "realized_minus_predicted")
        self.assertEqual(calibration["population_median_reversion_annual"], 0.0495)
        quintiles = calibration["er_quintiles"]
        assert isinstance(quintiles, list)
        first = quintiles[0]
        assert isinstance(first, dict)
        self.assertEqual(first["median_predicted_reversion_excess"], -0.02)
        self.assertEqual(first["median_realized_price_excess"], -0.04)
        self.assertEqual(first["calibration_error"], -0.02)

    def test_er_calibration_is_unchanged_when_only_carry_changes(self) -> None:
        forwards = [_forward_row(f"36{i:02d}", i / 1000) for i in range(100)]

        def panel_with_carry(carry: float) -> list[PanelRow]:
            return [
                _panel_row(
                    f"36{i:02d}",
                    per_trailing=10.0,
                    er_annual=i / 1000 + carry,
                    er_reversion_annual=i / 1000,
                    er_carry_annual=carry,
                )
                for i in range(100)
            ]

        low_carry = evaluate_cohorts(
            {"2025-06-30": panel_with_carry(0.01)},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )
        high_carry = evaluate_cohorts(
            {"2025-06-30": panel_with_carry(0.20)},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )

        self.assertEqual(
            low_carry["6m"]["cohorts"][0]["er_calibration"],
            high_carry["6m"]["cohorts"][0]["er_calibration"],
        )

    def test_er_level_calibration_compares_absolute_annual_total_return(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(100):
            ticker = f"37{i:02d}"
            reversion = i / 1000
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0,
                    er_annual=reversion + 0.02,
                    er_reversion_annual=reversion,
                    er_carry_annual=0.02,
                )
            )
            forwards.append(
                _forward_row(
                    ticker,
                    reversion,
                    total_return=reversion + 0.04,
                    horizon="1y",
                )
            )

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])

        cohort = result["1y"]["cohorts"][0]
        level = cohort["er_level_calibration"]
        assert isinstance(level, dict)
        self.assertEqual(level["prediction_basis"], "er_annual_absolute")
        self.assertEqual(
            level["realized_basis"],
            "fy_actual_dividend_total_return_annualized_absolute",
        )
        quintiles = level["er_quintiles"]
        assert isinstance(quintiles, list)
        first = quintiles[0]
        self.assertEqual(first["max_predicted_er_annual"], 0.039)
        self.assertEqual(first["median_predicted_er_annual"], 0.0295)
        self.assertEqual(first["median_realized_total_return_annual"], 0.0495)
        self.assertEqual(first["calibration_error_annual"], 0.02)
        self.assertEqual(first["median_predicted_reversion_annual"], 0.0095)
        self.assertEqual(first["median_predicted_carry_annual"], 0.02)
        self.assertEqual(first["median_realized_price_return_annual"], 0.0095)
        self.assertEqual(first["median_realized_dividend_contribution_annual"], 0.04)
        self.assertEqual(cohort["metric_statuses"]["er_level_calibration"], "eligible")

    def test_er_level_context_materializes_evidence_complete_cohorts(self) -> None:
        asof = "2020-01-31"
        panel = [
            _panel_row(
                f"39{index:02d}",
                per_trailing=10.0,
                er_annual=index / 1000,
                er_reversion_annual=index / 2000,
                er_carry_annual=index / 2000,
            )
            for index in range(100)
        ]
        forwards = [
            _forward_row(
                row.ticker,
                index / 100 - 0.01,
                total_return=index / 100,
                horizon=horizon,
            )
            for horizon in ("3y", "5y")
            for index, row in enumerate(panel)
        ]
        panel = [replace(row, asof=asof) for row in panel]
        forwards = [
            replace(
                row,
                asof=asof,
                entry_date=asof,
                target_date="2023-01-31" if row.horizon == "3y" else "2025-01-31",
                exit_date="2023-01-31" if row.horizon == "3y" else "2025-01-31",
            )
            for row in forwards
        ]
        evaluation: dict[str, object] = {
            "screening_rules_hash": "rules-hash-v1",
            "er_model_version": "expected-return-v1",
            "scope": {
                "run_purpose": "empirical_change_evidence",
                "required_asofs": [asof],
                "decision_subject": "estimator_policy",
                "required_metrics": [
                    "er_level_calibration",
                ],
                "mandatory_metrics": ["er_calibration", "er_level_calibration"],
            },
            "evidence_readiness": {
                "evidence_status": "eligible",
                "evidence_complete": True,
            },
            "cohort_integrity": [
                {
                    "asof": asof,
                    "horizon": horizon,
                    "integrity_status": "eligible",
                    "metric_statuses": dict.fromkeys(
                        (*ESTIMATOR_POLICY_MANDATORY_METRICS, "er_level_calibration"), "eligible"
                    ),
                }
                for horizon in ("3y", "5y")
            ],
            "results": {
                horizon: {
                    "cohorts": [
                        {
                            "asof": asof,
                            "metric_statuses": {"er_level_calibration": "eligible"},
                            "coverage": {
                                "master_snapshot_status": "exact_date",
                                "survivorship_coverage_status": "complete",
                                "adjustment_factor_coverage": "complete",
                                "input_range_clamped": False,
                                "candidate_partition_complete": True,
                            },
                        }
                    ]
                }
                for horizon in ("3y", "5y")
            },
        }

        context = build_er_distribution_context(
            evaluation,
            {asof: panel},
            {asof: forwards},
            generated_at=datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        ErCalibrationContextArtifact.model_validate(context)

        self.assertEqual(context["reference_horizon"], "3y")
        self.assertEqual(context["valid_through"], "2026-09-16")
        self.assertEqual(context["schema_version"], 4)
        common = context["common_window"]
        assert isinstance(common, dict)
        self.assertEqual(common["cohort_count"], 1)
        self.assertEqual(
            [item["horizon"] for item in common["horizons"] if isinstance(item, dict)],
            ["3y", "5y"],
        )
        horizons = context["horizons"]
        assert isinstance(horizons, list)
        first = horizons[0]
        assert isinstance(first, dict)
        self.assertEqual(first["cohort_count"], 1)
        bands = first["bands"]
        assert isinstance(bands, list)
        self.assertEqual(bands[0]["upper_er_annual"], 0.019)
        self.assertIsNone(bands[4]["upper_er_annual"])
        self.assertEqual(bands[5]["band_id"], "er_gte_8_5pct")
        self.assertEqual(bands[5]["median_n"], 15)
        bases = bands[0]["bases"]
        assert isinstance(bases, list)
        total = bases[0]
        assert isinstance(total, dict)
        stats = total["ticker_equal"]
        assert isinstance(stats, dict)
        self.assertAlmostEqual(stats["median"], (1.09 ** (1 / 3)) - 1, places=6)
        self.assertAlmostEqual(stats["q25"], (1.04 ** (1 / 3)) - 1, places=6)
        self.assertAlmostEqual(stats["q10"], (1.01 ** (1 / 3)) - 1, places=6)
        self.assertEqual(stats["trap_rate"], 1.0)

        integrity = evaluation["cohort_integrity"]
        assert isinstance(integrity, list)
        first_integrity = integrity[0]
        assert isinstance(first_integrity, dict)
        metric_statuses = first_integrity["metric_statuses"]
        assert isinstance(metric_statuses, dict)
        metric_statuses["er_calibration"] = "unresolved"
        with self.assertRaises(CalibrationContextError):
            build_er_distribution_context(evaluation, {asof: panel}, {asof: forwards})
        metric_statuses["er_calibration"] = "eligible"

        scope = evaluation["scope"]
        assert isinstance(scope, dict)
        scope["decision_subject"] = "candidate_discovery_approach"
        with self.assertRaisesRegex(
            CalibrationContextError, r"E\[r\] context requires estimator_policy evidence"
        ):
            build_er_distribution_context(evaluation, {asof: panel}, {asof: forwards})
        scope["decision_subject"] = "estimator_policy"

        evaluation["evidence_readiness"] = {
            "evidence_status": "unresolved",
            "evidence_complete": False,
        }
        partial = build_er_distribution_context(evaluation, {asof: panel}, {asof: forwards})
        self.assertFalse(partial["evidence_complete"])
        self.assertEqual(partial["required_asofs"], [asof])

    def test_er_level_calibration_does_not_treat_missing_total_return_as_zero(self) -> None:
        panel = [
            _panel_row(
                f"38{i:02d}",
                per_trailing=10.0,
                er_annual=0.05,
                er_reversion_annual=0.02,
                er_carry_annual=0.03,
            )
            for i in range(100)
        ]
        forwards = [_forward_row(f"38{i:02d}", 0.10, horizon="1y") for i in range(100)]

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])

        cohort = result["1y"]["cohorts"][0]
        self.assertEqual(cohort["er_level_calibration"], {})
        self.assertEqual(cohort["metric_statuses"]["er_level_calibration"], "unresolved")

    def test_cohort_with_insufficient_sample_remains_explicitly_unresolved(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0)]
        forwards = [_forward_row("1000", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["metric_calculation_status"], "unresolved")

    def test_candidate_partition_requires_exact_ticker_membership(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0), _panel_row("1001", per_trailing=10.0)]
        forwards = [_forward_row("9998", 0.1), _forward_row("9999", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        assert isinstance(cohort, dict)
        coverage = cohort["coverage"]
        assert isinstance(coverage, dict)
        self.assertFalse(coverage["candidate_partition_complete"])
        self.assertEqual(coverage["candidate_forward_missing_count"], 2)
        self.assertEqual(coverage["candidate_forward_extra_count"], 2)

    def test_horizon_output_has_only_current_contract_sections(
        self,
    ) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"4{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0 + i * 0.05,
                    sector_33="機械",
                )
            )
            forwards.append(_forward_row(ticker, 0.30 - i * 0.001))
        for i in range(120):
            ticker = f"5{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=4.0 + i * 0.05,
                    sector_33="サービス業",
                )
            )
            forwards.append(_forward_row(ticker, -0.30 - i * 0.001))

        result = evaluate_cohorts(
            {"2025-06-30": panel},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        self.assertEqual(
            set(horizon),
            {"evidence_role", "cohorts", "aggregate", "observation_dependence"},
        )

    def _rejected_metrics_message(self, required_metrics: list[str]) -> tuple[int, str]:
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            exit_code = calibration_evaluate_command(
                calibration_dir=Path(temp_dir),
                horizons=["3y", "5y"],
                output_path=Path(temp_dir) / "evaluation.yaml",
                stdout=StringIO(),
                run_purpose="empirical_change_evidence",
                required_asofs=["2021-06-30"],
                required_metrics=required_metrics,
            )
        return exit_code, errors.getvalue()

    def test_an_unregistered_required_metric_is_named_with_required_metrics_present(
        self,
    ) -> None:
        # Required subject metrics are present; report the actual unknown metric.
        exit_code, message = self._rejected_metrics_message(
            [*ESTIMATOR_POLICY_MANDATORY_METRICS, "not_a_registered_metric"]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("unknown required metrics: not_a_registered_metric", message)

    def test_subject_metrics_do_not_need_to_be_repeated_on_the_cli(self) -> None:
        exit_code, message = self._rejected_metrics_message(["er_calibration"])

        self.assertEqual(exit_code, 1)
        self.assertIn("calibration snapshot is missing", message)
        self.assertNotIn("must include", message)
        self.assertNotIn("unknown required metrics", message)

    def test_candidate_discovery_approach_subject_requires_one_known_approach(self) -> None:
        errors = StringIO()
        with TemporaryDirectory() as temp_dir, redirect_stderr(errors):
            exit_code = calibration_evaluate_command(
                calibration_dir=Path(temp_dir),
                horizons=["3y", "5y"],
                run_purpose="empirical_change_evidence",
                required_asofs=["2021-06-30"],
                required_metrics=list(ESTIMATOR_POLICY_MANDATORY_METRICS),
                decision_subject=CANDIDATE_DISCOVERY_APPROACH_SUBJECT,
                stdout=StringIO(),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("requires one --candidate-discovery-approach", errors.getvalue())

    def test_nomination_union_outcome_stays_registered(self) -> None:
        # `nomination_union_all` is emitted by every cohort, so leaving it out of the
        # registry would reject a preregistration that names an output it can see.
        self.assertIn("nomination_union_all", KNOWN_METRICS)

    def test_calibration_evaluate_command_marks_short_horizon_as_diagnostic(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(60):
            ticker = f"7{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0 + i * 0.05,
                    sector_33="サービス業",
                    er_annual=i / 1000 + 0.03,
                    er_reversion_annual=i / 1000,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, 0.30 - i * 0.001))
        for i in range(60):
            ticker = f"8{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=4.0 + i * 0.05,
                    sector_33="サービス業",
                    er_annual=(60 + i) / 1000 + 0.03,
                    er_reversion_annual=(60 + i) / 1000,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, -0.30 - i * 0.001))

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_panel(
                root,
                date(2025, 6, 30),
                tuple(panel),
                _panel_diagnostics(),
            )
            write_forward(
                root,
                date(2025, 6, 30),
                forwards,
            )
            output_path = root / "evaluation.yaml"

            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                output_path=output_path,
                stdout=StringIO(),
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            self.assertEqual(payload["metric_basis"], "price_return_only")
            self.assertEqual(
                payload["screening_rules_hash"],
                production_rules_contract_hash(_RULES.model_dump_json()),
            )
            self.assertEqual(payload["calibration_method_hash"], _RULES_HASH)
            self.assertNotIn("evidence_readiness", payload)
            results = payload["results"]
            assert isinstance(results, dict)
            horizon = results["6m"]
            assert isinstance(horizon, dict)
            aggregate = horizon["aggregate"]
            assert isinstance(aggregate, dict)
            self.assertEqual(aggregate["cohort_count"], 1)
            cohorts = horizon["cohorts"]
            assert isinstance(cohorts, list)
            calibration = cohorts[0]["er_calibration"]
            assert isinstance(calibration, dict)
            self.assertEqual(
                calibration["prediction_basis"],
                "er_reversion_annual_relative_to_population_median",
            )
            self.assertEqual(
                calibration["realized_basis"],
                "price_return_relative_to_population_median",
            )
            self.assertEqual(calibration["calibration_error_basis"], "realized_minus_predicted")

    def _long_horizon_cohort(
        self, extra: list[tuple[ForwardReturnRow, float | None]]
    ) -> tuple[list[PanelRow], list[ForwardReturnRow]]:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"7{i:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0 + i * 0.05))
            forwards.append(
                replace(
                    _forward_row(ticker, 0.10 - i * 0.001),
                    horizon="3y",
                    target_date="2028-06-30",
                    exit_date="2028-06-30",
                    adjustment_factor_coverage="complete",
                )
            )
        # panel の close が「asof に価格が付いていたか」の権威なので、分類の意図は
        # forward row ではなく panel 側の close で表す。
        for row, panel_close in extra:
            panel.append(_panel_row(row.ticker, per_trailing=None, close=panel_close))
            forwards.append(row)
        return panel, forwards

    def _reason_counts(
        self,
        panel: list[PanelRow],
        forwards: list[ForwardReturnRow],
        *,
        priced_master_without_universe_count: int = 0,
    ) -> tuple[dict[str, int], dict[str, object]]:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_panel(
                root,
                date(2025, 6, 30),
                tuple(panel),
                _panel_diagnostics(
                    master_snapshot_status="exact_date",
                    priced_master_without_universe_count=priced_master_without_universe_count,
                ),
            )
            write_forward(
                root,
                date(2025, 6, 30),
                forwards,
            )
            output_path = root / "evaluation.yaml"
            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["3y"],
                output_path=output_path,
                stdout=StringIO(),
            )
            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        coverage_summary = payload["evidence_coverage"]
        assert isinstance(coverage_summary, dict)
        counts = coverage_summary["reason_counts"]
        assert isinstance(counts, dict)
        cohorts = payload["results"]["3y"]["cohorts"]
        assert isinstance(cohorts, list)
        cohort_integrity = payload["cohort_integrity"]
        assert isinstance(cohort_integrity, list)
        self.assertEqual(len(cohort_integrity), 1)
        self.assertEqual(
            cohort_integrity[0]["metric_statuses"],
            {
                metric: cohorts[0]["metric_statuses"][metric]
                for metric in ESTIMATOR_POLICY_MANDATORY_METRICS
            },
        )
        coverage = cohorts[0]["coverage"]
        assert isinstance(coverage, dict)
        return counts, coverage

    def test_reason_histogram_only_counts_requested_cohorts(self) -> None:
        panel, forwards = self._long_horizon_cohort([])
        outside = date(2025, 5, 30)
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for day, master_status in (
                (date.fromisoformat(_ASOF), "exact_date"),
                (outside, "unavailable"),
            ):
                target = require_horizon("3y").target_date(day).isoformat()
                write_panel(
                    root,
                    day,
                    tuple(replace(row, asof=day.isoformat()) for row in panel),
                    replace(
                        _panel_diagnostics(master_snapshot_status=master_status),
                        asof=day.isoformat(),
                    ),
                )
                write_forward(
                    root,
                    day,
                    [
                        replace(
                            row,
                            asof=day.isoformat(),
                            entry_date=day.isoformat(),
                            target_date=target,
                            exit_date=target,
                        )
                        for row in forwards
                    ],
                )
            output = root / "evaluation.yaml"
            self.assertEqual(
                calibration_evaluate_command(
                    calibration_dir=root,
                    horizons=["3y"],
                    required_asofs=[_ASOF],
                    output_path=output,
                    stdout=StringIO(),
                ),
                0,
            )
            payload = yaml.safe_load(output.read_text(encoding="utf-8"))
        outside_result = next(
            item for item in payload["cohort_integrity"] if item["asof"] == outside.isoformat()
        )
        self.assertIn("master_snapshot", outside_result["blocking_reasons"])
        self.assertNotIn("master_snapshot", payload["evidence_coverage"]["reason_counts"])

    def test_unpriced_entry_is_disclosed_without_blocking_the_cohort(self) -> None:
        # panelに価格がなくproductionの有効価格scope外。未上場と断定せず、
        # forward欠測とは分けて件数を残す。
        unpriced = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9100",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date=None,
            exit_date=None,
            status="unresolved_missing_entry",
        )
        counts, coverage = self._reason_counts(*self._long_horizon_cohort([(unpriced, None)]))

        self.assertEqual(coverage["entry_unpriced_at_asof_count"], 1)
        self.assertEqual(coverage["entry_price_gap_count"], 0)
        self.assertEqual(coverage["unpriced_exit_count"], 0)
        self.assertEqual([key for key in counts if key.startswith("entry_price_gap")], [])
        self.assertEqual([key for key in counts if key.startswith("unpriced_exit")], [])

    def test_entry_missing_for_a_priced_name_blocks_even_without_an_entry_date(self) -> None:
        # panelの価格で分析対象にした銘柄をforward欠測で無言に落とさない。
        # 観測価格があることと、実際に約定できることは別である。
        dropped = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9400",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date=None,
            exit_date=None,
            status="unresolved_missing_entry",
        )
        counts, coverage = self._reason_counts(*self._long_horizon_cohort([(dropped, 1200.0)]))

        self.assertEqual(coverage["entry_unpriced_at_asof_count"], 0)
        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(counts.get("entry_price_gap"), 1)

    def test_known_raw_price_is_not_lost_when_panel_valuation_is_unavailable(self) -> None:
        missing = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9400",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date=None,
            exit_date=None,
            status="unresolved_missing_entry",
        )
        panel, forwards = self._long_horizon_cohort([(missing, None)])
        panel[-1] = replace(panel[-1], population_coverage_status="priced_master_without_universe")
        counts, coverage = self._reason_counts(
            panel, forwards, priced_master_without_universe_count=1
        )
        self.assertEqual(coverage["entry_unpriced_at_asof_count"], 0)
        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(counts.get("entry_price_gap"), 1)

    def test_price_gap_and_unpriced_exit_are_named_as_separate_blockers(self) -> None:
        # panel で価格を観測した銘柄の entry 欠測と、exit 価格の欠測を区別する。
        price_gap = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9200",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date="2024-01-31",
            exit_date=None,
            status="unresolved_missing_entry",
        )
        unpriced_exit = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9300",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=True,
            entry_date="2025-06-30",
            exit_date="2026-02-27",
            status="unresolved_stale_exit",
        )
        counts, coverage = self._reason_counts(
            *self._long_horizon_cohort([(price_gap, 900.0), (unpriced_exit, 1100.0)])
        )

        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(coverage["unpriced_exit_count"], 1)
        self.assertEqual(counts.get("entry_price_gap"), 1)
        self.assertEqual(coverage["unclassified_unresolved_count"], 0)
        # An unresolved exit is counted separately; the specified imputations
        # determine whether it changes the cohort conclusion.
        self.assertIsNone(counts.get("unpriced_exit"))

    def test_priced_master_row_count_mismatch_blocks_fail_closed(self) -> None:
        panel, forwards = self._long_horizon_cohort([])
        panel[0] = replace(panel[0], population_coverage_status="priced_master_without_universe")

        counts, coverage = self._reason_counts(
            panel,
            forwards,
            priced_master_without_universe_count=0,
        )

        sensitivity = coverage["priced_master_without_universe"]
        assert isinstance(sensitivity, dict)
        self.assertEqual(sensitivity["excluded_count"], 1)
        self.assertEqual(counts.get("priced_master_without_universe_unmeasured"), 1)

    def test_candidate_partition_mismatch_blocks_fail_closed(self) -> None:
        panel, forwards = self._long_horizon_cohort([])
        forwards.pop()

        counts, coverage = self._reason_counts(panel, forwards)

        self.assertFalse(coverage["candidate_partition_complete"])
        self.assertEqual(coverage["candidate_forward_missing_count"], 1)
        self.assertEqual(counts.get("candidate_partition_incomplete"), 1)

    def test_unknown_unresolved_status_blocks_fail_closed(self) -> None:
        unknown = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9500",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date="2025-06-30",
            exit_date=None,
            status="unresolved_unknown",
            adjustment_factor_coverage="complete",
        )

        counts, coverage = self._reason_counts(*self._long_horizon_cohort([(unknown, None)]))

        self.assertEqual(coverage["unclassified_unresolved_count"], 1)
        self.assertEqual(counts.get("unclassified_unresolved"), 1)

    def test_priced_master_row_without_return_uses_stable_bracket(self) -> None:
        missing = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9400",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date="2025-06-30",
            exit_date=None,
            status="unresolved_missing_exit",
        )
        panel, forwards = self._long_horizon_cohort([(missing, None)])
        panel[-1] = replace(panel[-1], population_coverage_status="priced_master_without_universe")

        counts, coverage = self._reason_counts(
            panel,
            forwards,
            priced_master_without_universe_count=1,
        )

        sensitivity = coverage["priced_master_without_universe"]
        assert isinstance(sensitivity, dict)
        self.assertFalse(sensitivity["resolution_complete"])
        self.assertTrue(sensitivity["direction_stable"])
        self.assertNotIn("priced_master_without_universe_return_unresolved", counts)
        self.assertNotIn("priced_master_without_universe_flips_direction", counts)

    def test_empirical_change_evidence_requires_explicit_core_scope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exit_code = calibration_evaluate_command(
                calibration_dir=Path(temp_dir),
                run_purpose="empirical_change_evidence",
                stdout=StringIO(),
            )
        self.assertEqual(exit_code, 1)


class MissingExitSensitivityTests(unittest.TestCase):
    """Whether unresolved exits could change conclusions under the specified imputations."""

    @staticmethod
    def _cohort(
        *, missing_exit_ranks: tuple[int, ...]
    ) -> tuple[list[PanelRow], list[ForwardReturnRow]]:
        # A cohort where nominated names beat the rest, plus unresolved exits.
        # Missing prices alone do not establish delisting or actual recovery value.
        panel = [
            _panel_row(f"{9000 + index}", per_trailing=10.0) for index in range(MIN_AXIS_SAMPLE)
        ]
        forwards = [_forward_row(row.ticker, 0.0) for row in panel]
        for rank in (1, 2):
            panel.append(_panel_row(f"81{rank:02d}", per_trailing=10.0, rank=rank))
            forwards.append(_forward_row(f"81{rank:02d}", 0.30))
        for rank in missing_exit_ranks:
            ticker = f"82{rank:02d}"
            panel.append(_panel_row(ticker, per_trailing=10.0, rank=rank))
            forwards.append(
                ForwardReturnRow(
                    asof="2025-06-30",
                    ticker=ticker,
                    horizon="6m",
                    target_date="2025-12-29",
                    resolved=False,
                    price_return=None,
                    stale_price=False,
                    entry_date="2025-06-30",
                    exit_date=None,
                    status="unresolved_missing_exit",
                )
            )
        return panel, forwards

    def _sensitivity(self, *, missing_exit_ranks: tuple[int, ...]) -> dict[str, object]:
        panel, forwards = self._cohort(missing_exit_ranks=missing_exit_ranks)
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]  # type: ignore[index]
        return cohort["coverage"]["delisting_exclusion"]  # type: ignore[index]

    def test_a_conclusion_stays_stable_under_both_specified_imputations(self) -> None:
        # The missing-exit name is outside the nomination group.
        # That group stays ahead under both specified imputations.
        panel, forwards = self._cohort(missing_exit_ranks=())
        panel.append(_panel_row("8300", per_trailing=10.0))
        forwards.append(
            ForwardReturnRow(
                asof="2025-06-30",
                ticker="8300",
                horizon="6m",
                target_date="2025-12-29",
                resolved=False,
                price_return=None,
                stale_price=False,
                entry_date="2025-06-30",
                exit_date=None,
                status="unresolved_missing_exit",
            )
        )
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"]["delisting_exclusion"]  # type: ignore[index]

        self.assertEqual(sensitivity["excluded_count"], 1)
        self.assertTrue(sensitivity["direction_stable"])

    def test_a_conclusion_that_moves_between_the_ends_blocks_the_cohort(self) -> None:
        # Half the nominated group has unresolved exits. Imputed as total losses they drag
        # the group below the rest of the cohort; called neutral they do not. The
        # cohort cannot say which happened, so it does not get to conclude.
        sensitivity = self._sensitivity(missing_exit_ranks=(3, 4))

        self.assertEqual(sensitivity["excluded_count"], 2)
        self.assertFalse(sensitivity["direction_stable"])
        imputations = sensitivity["imputations"]
        self.assertLess(imputations["total_loss"]["nomination_union_all"], 0)
        self.assertGreater(imputations["neutral"]["nomination_union_all"], 0)

    def test_normalized_per_direction_flip_blocks_optional_evidence(self) -> None:
        as_reported = {"normalized_per_3fy": 0.1}
        imputed = {
            "total_loss": as_reported,
            "neutral": {"normalized_per_3fy": -0.1},
        }
        stability = _metric_direction_stability(as_reported, imputed)
        self.assertFalse(stability["normalized_per_3fy"])

    def test_margin_short_trap_flip_blocks_optional_evidence(self) -> None:
        passing = {
            "decile_spread_median": 0.03,
            "best_decile_trap_rate": 0.1,
            "deciles": [{"trap_rate": 0.2}],
        }
        trap_regression = {
            **passing,
            "best_decile_trap_rate": 0.3,
        }
        self.assertEqual(_margin_short_to_adv_adoption_sign(passing), 1.0)
        self.assertEqual(_margin_short_to_adv_adoption_sign(trap_regression), 0.0)

        as_reported = {"margin_short_to_adv": 1.0}
        imputed = {
            "total_loss": as_reported,
            "neutral": {"margin_short_to_adv": 0.0},
        }
        stability = _metric_direction_stability(as_reported, imputed)
        self.assertFalse(stability["margin_short_to_adv"])

        self.assertIsNone(_margin_short_to_adv_adoption_sign({"deciles": []}))

    def test_a_conclusion_supported_only_by_observed_returns_blocks_the_cohort(self) -> None:
        # Three of five nominated names have unresolved exits. The reported result
        # uses only the two observed outcomes; both imputations agree the group did
        # not lead. Comparing the two imputations to each other alone would call
        # that stable, which is the missing-outcome selection bias the rule exists to catch.
        sensitivity = self._sensitivity(missing_exit_ranks=(3, 4, 5))

        self.assertEqual(sensitivity["excluded_count"], 3)
        self.assertGreater(sensitivity["as_reported"]["nomination_union_all"], 0)
        imputations = sensitivity["imputations"]
        self.assertLess(imputations["total_loss"]["nomination_union_all"], 0)
        self.assertEqual(imputations["neutral"]["nomination_union_all"], 0.0)
        self.assertFalse(sensitivity["direction_stable"])

    def test_missing_exit_neutral_uses_only_the_reported_eligible_population(self) -> None:
        panel, forwards = self._cohort(missing_exit_ranks=(3,))
        for index in range(MIN_AXIS_SAMPLE * 2):
            ticker = f"7{index:03d}"
            panel.append(_panel_row(ticker, in_population=False))
            forwards.append(_forward_row(ticker, 100.0))
        # A benchmark or other forward-only ticker is not an eligible company either.
        forwards.append(_forward_row("1306", 100.0))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"][  # type: ignore[index]
            "delisting_exclusion"
        ]
        self.assertEqual(sensitivity["excluded_count"], 1)
        self.assertEqual(sensitivity["neutral_return"], 0.0)

    def test_a_cohort_without_missing_exits_needs_no_imputation(self) -> None:
        sensitivity = self._sensitivity(missing_exit_ranks=())

        self.assertEqual(sensitivity["excluded_count"], 0)
        self.assertTrue(sensitivity["direction_stable"])
        self.assertEqual(sensitivity["as_reported"], {})
        self.assertEqual(sensitivity["imputations"], {})


class PricedMasterWithoutUniverseSensitivityTests(unittest.TestCase):
    def test_a_direction_split_keeps_the_cohort_blocked(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(100):
            ticker = f"6{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0))
            forwards.append(_forward_row(ticker, index / 100))
        for index in range(5):
            ticker = f"7{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0, rank=index + 1))
            forwards.append(_forward_row(ticker, 0.52))
        for index in range(2):
            ticker = f"8{index:03d}"
            panel.append(
                replace(
                    _panel_row(ticker, per_trailing=None),
                    population_coverage_status="priced_master_without_universe",
                )
            )
            forwards.append(_forward_row(ticker, 1.0))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"][  # type: ignore[index]
            "priced_master_without_universe"
        ]

        self.assertEqual(sensitivity["excluded_count"], 2)
        self.assertEqual(sensitivity["resolved_target_count"], 2)
        self.assertTrue(sensitivity["resolution_complete"])
        self.assertEqual(sensitivity["as_reported"]["nomination_union_all"], 0.0)
        self.assertGreater(sensitivity["imputations"]["total_loss"]["nomination_union_all"], 0)
        self.assertFalse(sensitivity["direction_stable"])

    def test_an_unresolved_target_direction_split_is_not_stable(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(100):
            ticker = f"6{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0))
            forwards.append(_forward_row(ticker, index / 100))
        for index in range(5):
            ticker = f"7{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0, rank=index + 1))
            forwards.append(_forward_row(ticker, 0.52))
        for index in range(2):
            ticker = f"8{index:03d}"
            panel.append(
                replace(
                    _panel_row(ticker, per_trailing=None),
                    population_coverage_status="priced_master_without_universe",
                )
            )
            forwards.append(
                ForwardReturnRow(
                    asof="2025-06-30",
                    ticker=ticker,
                    horizon="6m",
                    target_date="2025-12-29",
                    resolved=False,
                    price_return=None,
                    stale_price=False,
                    entry_date="2025-06-30",
                    exit_date=None,
                    status="unresolved_missing_exit",
                )
            )

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"][  # type: ignore[index]
            "priced_master_without_universe"
        ]

        self.assertEqual(sensitivity["resolved_target_count"], 0)
        self.assertFalse(sensitivity["resolution_complete"])
        self.assertFalse(sensitivity["direction_stable"])
        self.assertEqual(sensitivity["as_reported"]["nomination_union_all"], 0.0)
        self.assertGreater(sensitivity["imputations"]["total_loss"]["nomination_union_all"], 0)


def test_a_stratum_needs_five_names_on_each_side_before_it_is_matched() -> None:
    """A stratum with fewer names on one side is skipped, not matched at whatever it has.

    Stratifying compares rows within coarse bands of the named quality variable;
    it does not isolate a causal axis effect. A stratum matched on two names against two carries
    the noise of four rows into a figure the reader treats as a controlled difference,
    so the stratum is dropped and its weight leaves the answer. The counts are literal:
    derived from the constant they would move with it and pin nothing.
    """

    def _rows(prefix: str, count: int, quality: float) -> list[PanelRow]:
        return [
            _panel_row(f"{prefix}{index:03d}", equity_ratio=quality, per_trailing=10.0)
            for index in range(count)
        ]

    # One stratum only: every row shares the quality value, so the median split puts
    # them all on the same side.
    def _control(high_count: int, low_count: int | None = None) -> dict[str, object]:
        high = _rows("9", high_count, 0.5)
        low = _rows("1", high_count if low_count is None else low_count, 0.5)
        excess = {
            **{row.ticker: 0.10 for row in high},
            **{row.ticker: 0.02 for row in low},
        }
        return _stratified_quality_control(high, low, excess, field_name="equity_ratio")

    matched = _control(5)
    assert matched["strata_used"] == 1
    assert matched["matched_weight"] == 5
    assert matched["stratified_median_excess_delta"] is not None

    skipped = _control(4)
    assert skipped["strata_used"] == 0
    assert skipped["matched_weight"] == 0
    assert skipped["stratified_median_excess_delta"] is None
    assert skipped["stratified_trap_rate_delta"] is None

    # The floor applies to both sides, and to each of them on its own: five names
    # matched against four is still four names carrying the comparison, whichever side
    # is the thin one. Checking one side only would let the mirror case through.
    for high_count, low_count in ((5, 4), (4, 5)):
        thin = _control(high_count, low_count)
        assert thin["strata_used"] == 0, (high_count, low_count)
        assert thin["stratified_median_excess_delta"] is None, (high_count, low_count)


def test_the_half_split_needs_thirty_pairs_before_it_reports_a_difference() -> None:
    """Below the floor the split is not measured, and says so rather than answering.

    Splitting a group by its own axis value measures a within-group association,
    not an effect isolated from group composition. A split of a dozen rows compares six
    against six. The market-fallback groups run 50-80 rows per cohort, which is why the
    floor sits where it does. The counts are literal: derived from the constant they
    would move with it and pin nothing.
    """

    def _pairs(count: int) -> list[tuple[float, float]]:
        # Cheap names outperform, so a measured split has a non-zero delta.
        return [(float(index), float(index) * 0.01) for index in range(count)]

    measured = _half_split_effect(_pairs(30))
    assert measured["n"] == 30
    assert measured["median_excess_delta"] is not None

    withheld = _half_split_effect(_pairs(29))
    assert withheld["n"] == 29
    assert withheld["median_excess_delta"] is None
    assert withheld["cheap_median_excess"] is None
    assert withheld["expensive_median_excess"] is None


@pytest.mark.parametrize("output_option", ["output_path", "context_output_path"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_evaluation_report_cannot_replace_its_input_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_option: str, alias: str
) -> None:
    import baibai_engine.cli.historical_calibration as cli
    from baibai_engine.machine_analysis.historical_evaluation.store import CURRENT_SNAPSHOT_NAME

    write_panel(tmp_path, date.fromisoformat(_ASOF), [_panel_row("1111")], _panel_diagnostics())
    write_forward(tmp_path, date.fromisoformat(_ASOF), [_forward_row("1111", 0.1)])
    snapshot = tmp_path / CURRENT_SNAPSHOT_NAME
    destination = snapshot
    if alias != "direct":
        destination = tmp_path / "report.yaml"
        if alias == "symlink":
            destination.symlink_to(snapshot)
        else:
            destination.hardlink_to(snapshot)
    before = snapshot.read_bytes()
    # This test owns output safety, not the separate context-evidence calculation.
    monkeypatch.setattr(cli, "build_er_distribution_context", lambda *args: {"synthetic": True})
    assert (
        calibration_evaluate_command(
            calibration_dir=tmp_path,
            horizons=["6m"],
            stdout=StringIO(),
            **{output_option: destination},
        )
        == 1
    )
    assert snapshot.read_bytes() == before


if __name__ == "__main__":
    unittest.main()
