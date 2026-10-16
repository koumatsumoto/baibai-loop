"""Fetch bulk native-record sources without L2 calculations."""

from datetime import date

from ..definitions import SeriesDefinition
from ..observations import NativeRecords
from .base import FetchContext, IndicatorsProviderError
from .boj_timeseries import BojTimeSeriesSource
from .cftc import CftcSource
from .ecb_fx import EcbFxSource
from .estat import EStatSource
from .estat_dashboard import EStatDashboardSource
from .frb_h15 import FrbH15Source
from .fred import FredSource
from .jquants_flows import JQuantsFlowsSource
from .jquants_indices import JQuantsIndicesSource
from .multpl import MultplSource
from .umich_sca import UmichScaSource


def fetch_native_records(
    series: SeriesDefinition, *, start: date, end: date, context: FetchContext
) -> NativeRecords:
    source: (
        CftcSource
        | BojTimeSeriesSource
        | FredSource
        | EcbFxSource
        | JQuantsFlowsSource
        | JQuantsIndicesSource
        | EStatDashboardSource
        | EStatSource
        | FrbH15Source
        | MultplSource
        | UmichScaSource
    )
    if series.provider == "boj_timeseries":
        source = BojTimeSeriesSource()
    elif series.provider == "umich_sca":
        source = UmichScaSource()
    elif series.provider == "fred_csv":
        source = FredSource()
    elif series.provider == "multpl":
        source = MultplSource()
    elif series.provider == "frb_h15":
        source = FrbH15Source()
    elif series.provider == "estat":
        source = EStatSource()
    elif series.provider == "estat_dashboard":
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
