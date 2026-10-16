from datetime import date
from decimal import Decimal

import pytest

from baibai_engine.position.price_grid import (
    common_stock_limit_is_valid,
    floor_common_stock_limit,
)

SESSION = date(2026, 10, 15)


def floor(price, **overrides):
    return floor_common_stock_limit(
        Decimal(price),
        **dict(target_session=SESSION, market_segment="Prime", is_common_stock=True)
        | overrides,
    )


@pytest.mark.parametrize(
    ("boundary", "prior_tick", "next_tick"),
    [
        (1000, 1, 1),
        (3000, 1, 5),
        (5000, 5, 10),
        (10000, 10, 10),
        (30000, 10, 50),
        (50000, 50, 100),
        (100000, 100, 100),
        (300000, 100, 500),
        (500000, 500, 1000),
        (1000000, 1000, 1000),
        (3000000, 1000, 5000),
        (5000000, 5000, 10000),
        (10000000, 10000, 10000),
        (30000000, 10000, 50000),
        (50000000, 50000, 100000),
    ],
)
def test_every_official_band_boundary(boundary, prior_tick, next_tick):
    boundary = Decimal(boundary)
    assert floor(boundary - Decimal("0.1")) == boundary - prior_tick
    assert floor(boundary) == boundary
    assert floor(boundary + Decimal("0.1")) == boundary
    assert floor(boundary + next_tick) == boundary + next_tick


def test_floor_rechecks_destination_band_and_never_exceeds_author_ceiling():
    assert floor("3000.1") == 3000
    assert floor("5000.1") == 5000
    assert floor("950.5") == 950  # TOPIX500 の細刻みでも共通 grid へ揃える。
    assert floor("950." + "9" * 50) == 950
    assert floor("1e40") == Decimal("1e40")
    assert floor("0.9") is None


@pytest.mark.parametrize("price", [True, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_or_boolean_price_is_rejected(price):
    with pytest.raises(ValueError, match="finite decimal"):
        floor_common_stock_limit(
            price, target_session=SESSION, market_segment="Prime", is_common_stock=True
        )


@pytest.mark.parametrize("price", ["0", "-1"])
def test_nonpositive_price_has_no_floor(price):
    with pytest.raises(ValueError, match="positive"):
        floor(price)
    assert not common_stock_limit_is_valid(
        Decimal(price), target_session=SESSION, market_segment="Prime", is_common_stock=True
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"market_segment": "TOKYO PRO MARKET"},
        {"is_common_stock": False},
        {"target_session": date(2026, 10, 13)},
        {"target_session": date(2027, 3, 1)},
    ],
)
def test_unconfirmed_classification_or_rule_period_has_no_terms(overrides):
    assert floor("1000", **overrides) is None


def test_rule_until_last_session_and_validity_use_same_grid():
    assert floor("1000", target_session=date(2027, 2, 26)) == 1000
    assert common_stock_limit_is_valid(
        Decimal("5000"), target_session=SESSION, market_segment="プライム", is_common_stock=True
    )
    assert not common_stock_limit_is_valid(
        Decimal("5001"), target_session=SESSION, market_segment="Prime", is_common_stock=True
    )
