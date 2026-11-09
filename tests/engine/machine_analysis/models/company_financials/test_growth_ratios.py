import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.profit import _loss_narrowing
from baibai_engine.machine_analysis.models.company_financials.metrics.ratios import _yoy_ratio


@pytest.mark.parametrize("current", [-20.0, -5.0, 0.0, 5.0])
def test_negative_base_does_not_express_a_reversed_growth_rate(current):
    assert _yoy_ratio(current, -10.0) is None


def test_loss_narrowing_and_positive_base_remain_distinct():
    assert _loss_narrowing(-5.0, -10.0) is True
    assert _yoy_ratio(-5.0, 10.0) == -1.5
    assert _yoy_ratio(15.0, 10.0) == 0.5
    assert _yoy_ratio(5.0, 0.0) is None
