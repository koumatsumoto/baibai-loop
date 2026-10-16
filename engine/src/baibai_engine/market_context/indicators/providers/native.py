"""Fetch bulk native-record sources without L2 calculations."""

from datetime import date

from ..definitions import SeriesDefinition
from ..observations import NativeRecords
from .base import FetchContext, IndicatorsProviderError
from .cftc import CftcSource
from .ecb_fx import EcbFxSource
from .estat_dashboard import EStatDashboardSource
from .jquants_flows import JQuantsFlowsSource
from .jquants_indices import JQuantsIndicesSource


def fetch_native_records(
    series: SeriesDefinition, *, start: date, end: date, context: FetchContext
) -> NativeRecords:
    source: CftcSource | EcbFxSource | JQuantsFlowsSource | JQuantsIndicesSource | EStatDashboardSource
    if series.provider == "estat_dashboard":
        source = EStatDashboardSource()
    elif series.provider == "cftc":
        source = CftcSource()
    elif series.provider == "ecb_fx":
        source = EcbFxSource()
    elif series.provider == "jquants_flows":
        source = JQuantsFlowsSource()
    elif series.provider == "jquants_indices":
        source = JQuantsIndicesSource()
    else:
        raise IndicatorsProviderError(f"unsupported bulk native provider: {series.provider}")
    return source.fetch_records(
        series, start=start, end=end, session=context.session, context=context
    )
