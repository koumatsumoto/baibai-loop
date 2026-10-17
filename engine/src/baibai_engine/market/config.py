from __future__ import annotations

from pathlib import Path

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

# 再生成可能な一時 cache root。CSV ZIP や任意 disclosure title 入力など、
# market storeから外れる補助ファイルだけを置く。
DEFAULT_CACHE_DIR = Path(".cache/screening")
# 取得・hydrateしたmarket作業コピー。ソースごとに原本JSON・ZIPと読取用の行を保持する。
DEFAULT_SQLITE_CACHE_DIR = MARKET_DB_PATH.parent
