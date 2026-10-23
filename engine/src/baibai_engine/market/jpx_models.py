"""JPX公開観測の純粋な型・予定選択とエラー。取得・保存処理は読み込まない。"""

from collections.abc import Mapping, Sequence
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
    # Dates a lower-precedence cohort file offered for a ticker another file had
    # already dated, or `None` when the snapshot was read back from SQLite and
    # no merge happened. Distinct from the `rejected` counters elsewhere in the
    # cache, which mean malformed rows and turn a coverage row `partial`.
    superseded_record_count: int | None

    @property
    def valid_record_count(self) -> int:
        return len(self.entries)

    @property
    def min_date(self) -> date:
        return min(entry.announcement_date for entry in self.entries)

    @property
    def max_date(self) -> date:
        return max(entry.announcement_date for entry in self.entries)


def index_calendar_announcements(
    entries: Sequence[JPXEarningsCalendarEntry],
) -> dict[str, date]:
    """Index the current schedule, rejecting contradictory dates for one ticker.

    The source publishes one current date per ticker. The physical SQLite key is
    ``(announcement_date, ticker)``, so it cannot enforce that source contract.
    """
    index: dict[str, date] = {}
    for entry in entries:
        seen = index.get(entry.ticker)
        if seen is not None and seen != entry.announcement_date:
            raise ValueError(
                f"earnings calendar has conflicting rows for {entry.ticker}: "
                f"{seen.isoformat()} and {entry.announcement_date.isoformat()}"
            )
        index[entry.ticker] = entry.announcement_date
    return index


def next_earnings_dates(
    entries: Sequence[JPXEarningsCalendarEntry], asof_date: date
) -> dict[str, date]:
    """Select current dates on or after as-of; a date alone is not a disclosure."""
    return {
        ticker: announcement_date
        for ticker, announcement_date in index_calendar_announcements(entries).items()
        if announcement_date >= asof_date
    }
