from __future__ import annotations

JPX_SPECIAL_CAUTION_SOURCE_NAME = "特別注意銘柄"

# JPX の公開規制情報は実行環境によらず同じ source を使う。
JPX_REGULATION_URLS = {
    JPX_SPECIAL_CAUTION_SOURCE_NAME: "https://www.jpx.co.jp/listing/measures/alert/index.html",
    "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/",
    "取引停止": "https://www.jpx.co.jp/markets/equities/suspended/",
    "上場廃止警告": "https://www.jpx.co.jp/listing/stocks/delisted/",
}
