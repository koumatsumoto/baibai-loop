"""Fetch bulk native-record sources without L2 calculations."""

from datetime import date

from baibai_engine.collection.economic.records import NativeRecords
from baibai_engine.collection.economic.sources import SourceDefinition

from .base import FetchContext, IndicatorsProviderError
from .boj import BojSource
from .boj_timeseries import BojTimeSeriesSource
from .cftc import CftcSource
from .ecb_fx import EcbFxSource
from .estat import EStatSource
from .estat_dashboard import EStatDashboardSource
from .frb_h15 import FrbH15Source
from .fred import FredSource
from .jquants_flows import JQuantsFlowsSource
from .jquants_indices import JQuantsIndicesSource
from .mof_jgb import MofJgbSource
from .multpl import MultplSource
from .nikkei_indexes import NikkeiIndexesSource
from .umich_sca import UmichScaSource
from .yahoo import YahooChartSource


def fetch_native_records(
    series: SourceDefinition, *, start: date, end: date, context: FetchContext
) -> NativeRecords:
    source: (
        CftcSource
        | BojSource
        | BojTimeSeriesSource
        | FredSource
        | EcbFxSource
        | JQuantsFlowsSource
        | JQuantsIndicesSource
        | EStatDashboardSource
        | EStatSource
        | FrbH15Source
        | MultplSource
        | MofJgbSource
        | YahooChartSource
        | UmichScaSource
        | NikkeiIndexesSource
    )
    if series.provider == "nikkei_indexes":
        source = NikkeiIndexesSource()
    elif series.provider == "yahoo":
        source = YahooChartSource()
    elif series.provider == "boj":
        source = BojSource()
    elif series.provider == "mof_jgb":
        source = MofJgbSource()
    elif series.provider == "boj_timeseries":
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
