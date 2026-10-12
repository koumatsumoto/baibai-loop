"""The new owners expose pure contracts and preserve current storage identities."""

import importlib.util
import subprocess
import sys


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
    'baibai_engine.external_assessment.tradingview.client',
    'baibai_engine.external_assessment.tradingview.auth',
    'baibai_engine.storage.observations',
    'baibai_engine.storage.lake.writer',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert contract.MODEL_VALUATION_TABLE == 'retail_model_valuation_observations'
assert tv_contract.TABLE == 'tradingview_forecast_snapshots'
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_removed_package_paths_have_no_compatibility_imports():
    for name in (
        "baibai_engine.appdb",
        "baibai_engine.market.sqlite",
        "baibai_engine.market.lake",
        "baibai_engine.market.retail_dynamics",
        "baibai_engine.market.tradingview",
        "baibai_engine.market.providers.jquants_decode",
        "baibai_batch.jobs.retail_daily",
    ):
        assert importlib.util.find_spec(name) is None


def test_storage_reader_does_not_load_provider_or_writer_implementations():
    code = """
import sys
from baibai_engine.market import jpx_models, jquants_models, jquants_decode
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
