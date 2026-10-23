from __future__ import annotations

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

# 取得・hydrateしたmarket作業コピー。ソースごとに原本JSON・ZIPと読取用の行を保持する。
DEFAULT_SQLITE_CACHE_DIR = MARKET_DB_PATH.parent
