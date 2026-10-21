"""Small native master fixtures; production validation/population floors are not bypassed there."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable


def insert_master_rows(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[str, str, str | None, str, str | None, str]],
) -> None:
    """Seed native records from date, ticker, name, market, sector and explicit ProdCat."""
    records = []
    for day, ticker, name, market, sector, product_category in rows:
        code = f"{ticker}0"
        record = {
            "Date": day,
            "Code": code,
            "CoName": name,
            "MktNm": market,
            "S33Nm": sector,
            "ProdCat": product_category,
        }
        records.append((day, code, json.dumps(record, ensure_ascii=False)))
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_master_snapshots "
        "(snapshot_date, security_code, record_json) VALUES (?, ?, ?)",
        records,
    )


TSE_33_SECTORS = frozenset(
    {
        "水産・農林業",
        "鉱業",
        "建設業",
        "食料品",
        "繊維製品",
        "パルプ・紙",
        "化学",
        "医薬品",
        "石油・石炭製品",
        "ゴム製品",
        "ガラス・土石製品",
        "鉄鋼",
        "非鉄金属",
        "金属製品",
        "機械",
        "電気機器",
        "輸送用機器",
        "精密機器",
        "その他製品",
        "電気・ガス業",
        "陸運業",
        "海運業",
        "空運業",
        "倉庫・運輸関連業",
        "情報・通信業",
        "卸売業",
        "小売業",
        "銀行業",
        "証券・商品先物取引業",
        "保険業",
        "その他金融業",
        "不動産業",
        "サービス業",
    }
)
