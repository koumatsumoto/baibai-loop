"""Trading-session projection decoded from the native holiday classification."""

from datetime import date

from pydantic.dataclasses import dataclass

from baibai_engine.machine_analysis.observations.prices.price_models import MODEL_CONFIG


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool
