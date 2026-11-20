"""Trading-session projection decoded from the native holiday classification."""

from datetime import date

from pydantic.dataclasses import dataclass

from baibai_engine.machine_analysis.observations.prices.price_models import MODEL_CONFIG


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool


def cash_equity_open(holiday_division: object) -> bool | None:
    """Decode a stored J-Quants schedule day; futures-only is closed for equities."""
    if holiday_division in ("1", "2"):
        return True
    if holiday_division in ("0", "3"):
        return False
    return None
