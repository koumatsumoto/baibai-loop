"""L2 の一時計算値を受け渡す契約。取得・保存には依存しない。"""

from __future__ import annotations

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
    quality_flags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Vintage is an instant, not the spelling of a provider's timezone.
        # Comparison uses a canonical UTC instant, independent of source spelling.
        if self.vintage_at is not None:
            if self.vintage_at.tzinfo is None or self.vintage_at.utcoffset() is None:
                raise ValueError("observation vintage_at must include a timezone")
            object.__setattr__(self, "vintage_at", self.vintage_at.astimezone(UTC))
