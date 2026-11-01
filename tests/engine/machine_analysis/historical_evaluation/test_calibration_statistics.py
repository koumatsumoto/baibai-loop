"""較正の順位統計と期間収益の年率換算。"""

from __future__ import annotations

import unittest

import pytest

from baibai_engine.machine_analysis.historical_evaluation.evaluation.statistics import (
    _spearman,
    annualize_return,
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


@pytest.mark.parametrize(
    ("value", "years", "expected"),
    [(0.21, 2.0, 0.1), (-0.75, 2.0, -0.5), (-1.0, 5.0, -1.0), (0.0, 3.0, 0.0)],
)
def test_geometric_annualization_keeps_gain_loss_and_total_loss(value, years, expected):
    assert annualize_return(value, years=years) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "years"),
    [(-1.01, 1.0), (0.2, 0.0), (0.2, -1.0), (float("nan"), 3.0), (float("inf"), 3.0)],
)
def test_unrepresentable_annual_return_stays_missing(value, years):
    assert annualize_return(value, years=years) is None
