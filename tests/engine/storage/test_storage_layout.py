"""Storage contracts stay independent of acquisition and publication services."""

import subprocess
import sys


def test_collection_http_and_mcp_adapters_do_not_load_analysis_domains():
    code = """
import sys
from baibai_engine.data_collection.transport import public_pages as equity_http
from baibai_engine.data_collection.providers.tradingview import receipts
for prefix in (
    'baibai_engine.machine_analysis',
    'baibai_engine.portfolio',
    'baibai_engine.investment_judgment',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert equity_http.MIN_REQUEST_INTERVAL_SECONDS == 2.0
assert callable(receipts.read_tool_results)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_pure_observation_and_lake_contracts_do_not_load_io():
    code = """
import sys
from baibai_engine.machine_analysis.observations.external_assessments import contract, models
from baibai_engine.machine_analysis.observations.external_assessments.tradingview import contract as tv_contract, observations
from baibai_engine.storage.snapshots import models as lake_models
for prefix in (
    'baibai_engine.data_collection.transport.public_pages',
    'baibai_jobs.acquisition.external_assessments',
    'baibai_engine.machine_analysis.observations.external_assessments.store',
    'baibai_jobs.maintenance.tradingview_context',
    'baibai_engine.storage.sqlite.source_cache',
    'baibai_engine.storage.snapshots.writer',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert contract.MODEL_VALUATION_TABLE == 'external_assessment_model_valuation_observations'
assert tv_contract.TABLE == 'tradingview_forecast_snapshots'
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_market_readers_do_not_load_provider_or_writer_implementations():
    code = """
import sys
from baibai_engine.machine_analysis.observations import jquants_decode
from baibai_engine.machine_analysis.observations.financials import jquants_models
from baibai_engine.machine_analysis.observations.financials import edinet_models
from baibai_engine.machine_analysis.observations.financials import reader as financial_reader
from baibai_engine.machine_analysis.observations.prices import calendar_reader, price_reader
from baibai_engine.machine_analysis.observations.stocks import reader as master_reader
for prefix in (
    'baibai_engine.machine_analysis.observations.jquants_writer',
    'baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild',
    'baibai_engine.machine_analysis.observations.financials.edinet_metrics',
    'baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert callable(financial_reader.read_fin_summaries)
assert callable(calendar_reader.read_market_calendar)
assert callable(price_reader.read_daily_bars)
assert callable(master_reader.read_master_snapshot)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_edinet_decoding_and_sqlite_reads_do_not_load_http_client():
    code = """
import sys
from baibai_engine.machine_analysis.observations.financials import edinet_decode
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import store
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import csv as edinet_csv
for prefix in (
    'baibai_jobs.maintenance.source_refresh.edinet',
    'requests',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert edinet_decode.normalize_metric_record({'ticker': '72030', 'cash': 0}).cash == 0
assert store.normalize_metric_record is edinet_decode.normalize_metric_record
"""
    subprocess.run([sys.executable, "-c", code], check=True)
