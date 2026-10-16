"""Exact-date domestic ordinary-stock scope from native product category and issue code."""

from __future__ import annotations

from datetime import date

from baibai_engine.market.universe import ELIGIBLE_MARKETS

_MARKETS = tuple(sorted(ELIGIBLE_MARKETS))
_MARKET_PARAMS = ", ".join(f":market{index}" for index in range(len(_MARKETS)))
COMMON_STOCK_SNAPSHOT_SQL = (
    "SELECT substr(security_code, 1, 4) AS ticker FROM jquants_master_snapshots "
    "WHERE snapshot_date = :asof AND substr(security_code, 5, 1) = '0' "
    "AND json_extract(record_json, '$.ProdCat') = '011' "
    f"AND UPPER(json_extract(record_json, '$.MktNm')) IN ({_MARKET_PARAMS})"
)


def scope_parameters(asof: date) -> dict[str, str]:
    return {
        "asof": asof.isoformat(),
        **{f"market{index}": value for index, value in enumerate(_MARKETS)},
    }
