from __future__ import annotations

from pathlib import Path

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

# 再生成可能な一時 cache root。CSV ZIP や任意 disclosure title 入力など、
# SQLite 正本から外れる補助ファイルだけを置く。
DEFAULT_CACHE_DIR = Path(".cache/screening")
# local canonical store。provider fetch は raw JSON を経由せず
# この SQLite に正規化済み rows と source_coverage を直接保存する。
DEFAULT_SQLITE_CACHE_DIR = MARKET_DB_PATH.parent
JQUANTS_CLIENT_V2_METHODS = (
    "get_eq_master",
    "get_eq_bars_daily_range",
    "get_fin_summary_range",
    "get_mkt_calendar",
    "get_mkt_margin_interest",
    "get_mkt_short_sale_report",
)
