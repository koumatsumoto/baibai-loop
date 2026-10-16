"""経済時系列値の純粋な受渡し契約。取得・保存・Readingへ依存しない。

L1の観測と、読取中だけ存在するL2の計算値が同じ値形状を使う。
この型のインスタンスであること自体は、提供元が公表した事実の証明ではない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime


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

    def __post_init__(self) -> None:
        # Vintage is an instant, not the spelling of a provider's timezone.
        # SQLite revision keys and ordering use its canonical UTC representation.
        if self.vintage_at is not None:
            if self.vintage_at.tzinfo is None or self.vintage_at.utcoffset() is None:
                raise ValueError("observation vintage_at must include a timezone")
            object.__setattr__(self, "vintage_at", self.vintage_at.astimezone(UTC))


# A complete source response grouped by its native observation dates.
type NativeRecords = Mapping[date, Sequence[Mapping[str, object]]]
