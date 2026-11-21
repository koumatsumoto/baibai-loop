"""JPX公開観測の純粋な型・予定選択とエラー。取得・保存処理は読み込まない。"""

from collections.abc import Mapping
from datetime import date

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


class JPXProviderError(RuntimeError):
    """Raised when required JPX sources are unavailable or stored inputs are corrupt."""


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXRegulationSnapshot:
    flags_by_ticker: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    source_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXEarningsCalendarEntry:
    ticker: str
    announcement_date: date


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXEarningsCalendarSnapshot:
    entries: tuple[JPXEarningsCalendarEntry, ...]
    source_urls: tuple[str, ...]
    raw_record_count: int
    excluded_record_count: int
    # Dates from lower-priority files for tickers already handled by a newer file.
    superseded_record_count: int

    @property
    def valid_record_count(self) -> int:
        return len(self.entries)

    @property
    def min_date(self) -> date:
        return min(entry.announcement_date for entry in self.entries)

    @property
    def max_date(self) -> date:
        return max(entry.announcement_date for entry in self.entries)
