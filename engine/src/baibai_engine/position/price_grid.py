"""東証普通株の提案価格を公式「その他」呼値の保守的共通 grid へ揃える。

TOPIX500 の最良呼値や注文受理を保証するものではない。
master 原本の商品区分と銘柄コードで普通株を確認し、
市場と合わせて適用対象を限定する。2027 年制度変更以降の新条件には適用しない。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

from baibai_engine.market.universe import ELIGIBLE_MARKETS
from baibai_engine.valuation.scenario import finite_decimal

PRICE_GRID_RULE_ID = "tse-common-conservative-20261014"
PRICE_GRID_SOURCE_URL = "https://www.jpx.co.jp/equities/trading/domestic/07.html"
PRICE_GRID_CONFIRMED_AT = date(2026, 10, 14)
PRICE_GRID_VALID_UNTIL = date(2027, 3, 1)

# JPX 公式の「その他の銘柄」。価格帯上限は inclusive。
_TICKS: tuple[tuple[Decimal | None, Decimal], ...] = tuple(
    (None if upper is None else Decimal(upper), Decimal(tick))
    for upper, tick in (
        (1000, 1),
        (3000, 1),
        (5000, 5),
        (10000, 10),
        (30000, 10),
        (50000, 50),
        (100000, 100),
        (300000, 100),
        (500000, 500),
        (1000000, 1000),
        (3000000, 1000),
        (5000000, 5000),
        (10000000, 10000),
        (30000000, 10000),
        (50000000, 50000),
        (None, 100000),
    )
)


def _available(*, target_session: date, market_segment: str, is_common_stock: bool) -> bool:
    return (
        PRICE_GRID_CONFIRMED_AT <= target_session < PRICE_GRID_VALID_UNTIL
        and market_segment.strip().upper() in ELIGIBLE_MARKETS
        and is_common_stock
    )


def _tick(price_yen: Decimal) -> Decimal:
    return next(tick for upper, tick in _TICKS if upper is None or price_yen <= upper)


def floor_common_stock_limit(
    price_yen: Decimal, *, target_session: date, market_segment: str, is_common_stock: bool
) -> Decimal | None:
    """Floor an author's price ceiling; unavailable classification/rule gives no terms."""
    price = finite_decimal(price_yen)
    if price <= 0:
        raise ValueError("price_yen must be positive")
    if not _available(
        target_session=target_session,
        market_segment=market_segment,
        is_common_stock=is_common_stock,
    ):
        return None
    tick = _tick(price)
    with localcontext() as context:
        context.prec = max(50, len(price.as_tuple().digits), price.adjusted() + 1)
        result = (price // tick) * tick
        if result <= 0 or result % _tick(result) != 0:
            return None
    return result


def common_stock_limit_is_valid(
    price_yen: Decimal, *, target_session: date, market_segment: str, is_common_stock: bool
) -> bool:
    """Check a positive finite price against the grid at the proposed session."""
    price = finite_decimal(price_yen)
    return (
        price > 0
        and floor_common_stock_limit(
            price,
            target_session=target_session,
            market_segment=market_segment,
            is_common_stock=is_common_stock,
        )
        == price
    )
