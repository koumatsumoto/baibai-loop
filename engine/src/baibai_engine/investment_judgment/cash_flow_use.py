"""普通株主から見た報告CFの読み方。元の財務・業種・算術精度は書き換えない。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

# 業種不明を「非金融」と扱わない。J-QuantsのS33Nmには半角の中点もある。
_OPERATING_SECTORS = frozenset(
    [
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
        "不動産業",
        "サービス業",
    ]
)
_LIMITS = {
    "銀行業": (
        "bank_funding_flows",
        (
            "預金と貸出は銀行の営業資金。営業CFや設備取得後CFの符号・対時価比は分配余力ではない。"
            "政策保有株の売却益・信用費用を分けた継続利益、CET1の経営目標と必要資本、"
            "普通株の実分配と残りの配当権利を確認する。"
        ),
    ),
    "保険業": (
        "insurer_funding_flows",
        (
            "保険料・保険金と責任準備金の変動を普通株の分配余力にしない。"
            "保険・運用の継続利益、必要な支払余力資本と普通株への実分配を確認する。"
        ),
    ),
}
_OTHER_FINANCE = (
    "financing_business_review",
    (
        "顧客資金・貸付・リースなどの影響は事業構成で異なる。"
        "業態、必要資本、継続利益と普通株への実分配が分かるまで報告CFの符号を判断に使わない。"
    ),
)
_UNKNOWN = (
    "business_model_unverified",
    (
        "当時の同社業種を確定できない。事業資金と顧客資金、必要資本、普通株への分配を確かめるまで"
        "報告CFの符号を判断に使わない。"
    ),
)


@dataclass(frozen=True)
class CashFlowLimit:
    reason: str
    reading: str
    sector: str | None
    sector_date: str | None

    def explanation(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "sector_33": self.sector,
            "master_snapshot_date": self.sector_date,
            "interpretation": self.reading,
        }


def cash_flow_limit(market: object, *, ticker: str, as_of: date) -> CashFlowLimit | None:
    """使うのは同じ固定入力の当時の個社masterだけ。銘柄名や他の会社から類推しない。"""
    master = market.get("master") if isinstance(market, Mapping) else None
    sector = day = None
    if (
        isinstance(market, Mapping)
        and market.get("ticker", ticker) == ticker
        and isinstance(master, Mapping)
        and master.get("ticker", ticker) == ticker
    ):
        stamp, raw = master.get("snapshot_date"), master.get("sector_33")
        if isinstance(stamp, str) and isinstance(raw, str):
            try:
                measured = date.fromisoformat(stamp)
            except ValueError:
                pass
            else:
                if measured <= as_of and raw.strip():
                    sector, day = raw.strip(), measured.isoformat()
    label = sector.replace("･", "・").replace("、", "・") if sector else None
    if label in _OPERATING_SECTORS:
        return None
    reason, reading = (
        _OTHER_FINANCE
        if label in {"証券・商品先物取引業", "その他金融業"}
        else _LIMITS.get(label or "", _UNKNOWN)
    )
    return CashFlowLimit(reason, reading, sector, day)


def label_reported_cash(
    view: dict[str, Any],
    limit: CashFlowLimit,
    fields: Mapping[str, str],
) -> None:
    """判断用のcopyだけで比率と原表精度を同じ根拠に表示する。欠測/0も補作しない。"""
    if any(key in view for key in fields):
        view["reported_cash_flow_basis"] = {
            **limit.explanation(),
            **{out: view.pop(key) for key, out in fields.items() if key in view},
        }


GENERATED_YIELDS = {
    "fcf_yield": "reported_cfo_less_capex_to_market_cap",
    "ttm_quality_fcf_yield": "reported_fcf_period_quality",
    "ocf_yield": "reported_cfo_to_market_cap",
    "ttm_quality_ocf_yield": "reported_ocf_period_quality",
}
