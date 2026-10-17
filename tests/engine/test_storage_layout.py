"""Storage contracts stay independent of acquisition and publication services."""

import subprocess
import sys


def test_collection_http_and_mcp_adapters_do_not_load_analysis_domains():
    code = """
import sys
from baibai_engine.collection.equity_web import http as equity_http
from baibai_engine.collection.tradingview import auth, authorize, client
for prefix in (
    'baibai_engine.external_assessment',
    'baibai_engine.market',
    'baibai_engine.market_context',
    'baibai_engine.valuation',
    'baibai_engine.screening',
    'baibai_engine.position',
    'baibai_engine.research',
    'baibai_engine.read_api',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert equity_http.MIN_REQUEST_INTERVAL_SECONDS == 2.0
assert client.SERVER_URL == 'https://mcp.tradingview.com/mcp'
assert auth.SECRET_NAME == 'TRADINGVIEW_OAUTH_STATE'
assert callable(authorize.authorize)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_pure_observation_and_lake_contracts_do_not_load_io():
    code = """
import sys
from baibai_engine.external_assessment.observations import contract, models
from baibai_engine.external_assessment.tradingview import contract as tv_contract, observations
from baibai_engine.storage.lake import models as lake_models
for prefix in (
    'baibai_engine.external_assessment.acquisition',
    'baibai_engine.external_assessment.store',
    'baibai_engine.external_assessment.tradingview.collector',
    'baibai_engine.collection.tradingview.client',
    'baibai_engine.collection.tradingview.auth',
    'baibai_engine.storage.observations',
    'baibai_engine.storage.lake.writer',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert contract.MODEL_VALUATION_TABLE == 'external_assessment_model_valuation_observations'
assert tv_contract.TABLE == 'tradingview_forecast_snapshots'
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_storage_reader_does_not_load_provider_or_writer_implementations():
    code = """
import sys
from baibai_engine.market import edinet_models, jpx_models, jquants_models, jquants_decode
from baibai_engine.storage.observations import reader
for prefix in (
    'baibai_engine.market.providers',
    'baibai_engine.market.edinet_metrics',
    'baibai_engine.storage.observations.ingest',
    'baibai_engine.storage.observations.jquants',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert reader.JPXEarningsCalendarEntry is jpx_models.JPXEarningsCalendarEntry
assert reader.JQuantsProviderError is jquants_decode.JQuantsProviderError
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_edinet_decoding_and_sqlite_reads_do_not_load_http_client():
    code = """
import sys
from baibai_engine.market import edinet_decode
from baibai_engine.market.edinet_metrics import store
from baibai_engine.market.providers import edinet_csv
for prefix in (
    'baibai_engine.market.edinet_refresh',
    'requests',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert edinet_decode.normalize_metric_record({'ticker': '72030', 'cash': 0}).cash == 0
assert store.normalize_metric_record is edinet_decode.normalize_metric_record
"""
    subprocess.run([sys.executable, "-c", code], check=True)
