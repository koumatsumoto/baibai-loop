"""Fetch bulk native-record sources without L2 calculations."""

from datetime import date

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.providers.boj.timeseries import BojTimeSeriesSource
from baibai_engine.data_collection.providers.boj.workbooks import BojSource
from baibai_engine.data_collection.providers.cftc import CftcSource
from baibai_engine.data_collection.providers.ecb.fx import EcbFxSource
from baibai_engine.data_collection.providers.estat.api import EStatSource
from baibai_engine.data_collection.providers.estat.dashboard import EStatDashboardSource
from baibai_engine.data_collection.providers.federal_reserve.h15 import FrbH15Source
from baibai_engine.data_collection.providers.fred import FredSource
from baibai_engine.data_collection.providers.jquants.flows import JQuantsFlowsSource
from baibai_engine.data_collection.providers.jquants.indices import JQuantsIndicesSource
from baibai_engine.data_collection.providers.mof.jgb import MofJgbSource
from baibai_engine.data_collection.providers.multpl import MultplSource
from baibai_engine.data_collection.providers.nikkei.indices import NikkeiIndexesSource
from baibai_engine.data_collection.providers.university_of_michigan.sentiment import UmichScaSource
from baibai_engine.data_collection.providers.yahoo import YahooChartSource
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import FetchContext, IndicatorsProviderError


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
