"""Missing markers stay distinct from legitimate false/zero scalar values."""

from decimal import Decimal

import pytest

from baibai_engine.primitives.missing import is_missing_scalar


@pytest.mark.parametrize(
    "value", [None, "", " ", "NaT", "nan", "null", "<NA>", float("nan"), Decimal("NaN")]
)
def test_missing_scalar(value):
    assert is_missing_scalar(value)


@pytest.mark.parametrize("value", [0, 0.0, False, "0", "false", "NULL", "NaN", "value"])
def test_nonmissing_scalar(value):
    assert not is_missing_scalar(value)
