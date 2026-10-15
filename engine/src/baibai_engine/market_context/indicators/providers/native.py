"""Fetch the two bulk native-record sources without L2 calculations."""

from datetime import date

from ..definitions import SeriesDefinition
from ..observations import NativeRecords
from .base import FetchContext, IndicatorsProviderError
from .cftc import CftcSource
from .ecb_fx import EcbFxSource


def fetch_native_records(
    series: SeriesDefinition, *, start: date, end: date, context: FetchContext
) -> NativeRecords:
    source: CftcSource | EcbFxSource
    if series.provider == "cftc":
        source = CftcSource()
    elif series.provider == "ecb_fx":
        source = EcbFxSource()
    else:
        raise IndicatorsProviderError(f"unsupported bulk native provider: {series.provider}")
    return source.fetch_records(
        series, start=start, end=end, session=context.session, context=context
    )
