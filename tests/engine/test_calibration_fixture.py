from __future__ import annotations

import pytest

from tests.helpers.calibration_store import forward_row


@pytest.mark.parametrize("total", [0.0, "0", 0.1])
def test_forward_fixture_preserves_an_explicit_total_return(total: float | str) -> None:
    row = forward_row(
        "2020-01-31",
        "1234",
        "1y",
        price_return=-0.1,
        total_return_status="resolved",
        total_return=total,
    )
    assert row.total_return == float(total)
    assert row.realized_dividend_sum == pytest.approx(float(total) + 0.1)


def test_forward_fixture_defaults_only_an_absent_total_to_price_return() -> None:
    row = forward_row(
        "2020-01-31", "1234", "1y", price_return=-0.1, total_return_status="resolved"
    )
    assert row.total_return == -0.1
    assert row.realized_dividend_sum == 0.0
