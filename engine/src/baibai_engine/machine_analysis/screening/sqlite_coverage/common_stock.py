"""Exact-date domestic ordinary-stock scope from native product category and issue code."""

from __future__ import annotations

import json
from datetime import date

from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS

_MARKETS = tuple(sorted(ELIGIBLE_MARKETS))
COMMON_STOCK_SNAPSHOT_SQL = (
    "SELECT substr(security_code, 1, 4) AS ticker FROM jquants_master_snapshots "
    "WHERE snapshot_date = :asof AND substr(security_code, 5, 1) = '0' "
    "AND json_extract(record_json, '$.ProdCat') = '011' "
    "AND UPPER(json_extract(record_json, '$.MktNm')) "
    "IN (SELECT value FROM json_each(:markets_json))"
)


def scope_parameters(asof: date) -> dict[str, str]:
    return {
        "asof": asof.isoformat(),
        "markets_json": json.dumps(_MARKETS, ensure_ascii=False),
    }
