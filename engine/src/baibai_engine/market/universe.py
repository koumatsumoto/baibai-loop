"""Shared TSE common-stock classification, independent of bar-history eligibility."""

# The scope is the main domestic markets: everything a private investor can buy on
# ordinary terms, less TOKYO PRO MARKET and the residual segments. The exchange
# renamed its segments in April 2022, so a point-in-time master from before then
# carries the older names for the same markets. Both vocabularies are listed
# because the scope is about which markets, not about what they are called; a
# current master never carries the older names, so historical replay is the only
# place they appear.
ELIGIBLE_MARKETS = {
    "PRIME",
    "STANDARD",
    "GROWTH",
    "プライム",
    "スタンダード",
    "グロース",
    "東証一部",
    "東証二部",
    "マザーズ",
    "JASDAQ スタンダード",
    "JASDAQ グロース",
}

# 東証 33 業種。分類は普通株にのみ付与されるので、これに載らない銘柄は普通株ではない。
# `is_common_stock` は判別材料にならない: master の証券種別 field を J-Quants が返さない
# ため取得側の判定が常に真へ落ち、store の 568,329 行すべてで 1 になっている。業種分類は
# source が実際に答えている唯一の識別子なので、instrument type の除外はここから作る。
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


class UniverseSourceDriftError(RuntimeError):
    """業種分類の語彙が、母集団の定義が知っている集合から外れたときに送出する。"""


# 普通株でない銘柄に付く分類。33 業種と併せて、source が返しうる語彙の全体を成す。
# 実 store の 568,329 行・136 snapshot で観測される値はこの和集合と厳密に一致する。
NON_COMMON_STOCK_SECTORS = frozenset({"その他"})
