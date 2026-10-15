"""経済時系列値の純粋な受渡し契約。取得・保存・Readingへ依存しない。

L1の観測と、読取中だけ存在するL2の計算値が同じ値形状を使う。
この型のインスタンスであること自体は、提供元が公表した事実の証明ではない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class ObservationRecord:
    series_id: str
    observed_at: date
    value: float
    unit: str
    source_url: str
    period_start: date | None = None
    period_end: date | None = None
    vintage_at: datetime | None = None
    fetch_status: str = "ok"


# A complete source response grouped by its native observation dates.
type NativeRecords = Mapping[date, Sequence[Mapping[str, object]]]
