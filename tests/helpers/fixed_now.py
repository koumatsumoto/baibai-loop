"""時刻依存のfixtureを作成当時の固定時点で検証する。

鮮度・有効期限をwall clockへ依存させず、保存されたfixtureとその検証時点の
組合せを維持する。新しいfixtureは自身のas_ofに合う時点を明示する。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

FIXED_NOW = datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
