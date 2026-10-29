from contextlib import closing

from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview import DAY
from tests.helpers.tradingview import store as market_store

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.acquisition.source_inputs import input_key, load_input, publish_inputs


def test_failed_market_group_retains_prior_input_and_still_updates_economic_groups(
    tmp_path, monkeypatch
):
    remote = MemoryR2Store()
    market = market_store(tmp_path / "market.sqlite", 2)
    economic = tmp_path / "economic.sqlite"
    with closing(initialize_database(economic)):
        pass
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ("jquants-financial",)
    )
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    generations = {
        "lake_release_id": "fixed",
        "lake_manifest_sha256": "a" * 64,
        "economic_etag": "etag",
    }
    assert (
        publish_inputs(
            remote,
            market=market,
            economic=economic,
            identity="first",
            asof=DAY,
            generations=generations,
        )["failed"]
        == []
    )
    previous = remote.values[input_key("jquants-financial")].body

    def fail_market_input(*_args, **_kwargs):
        raise ValueError("unavailable market input")

    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.export_source_input", fail_market_input)
    report = publish_inputs(
        remote,
        market=market,
        economic=economic,
        identity="second",
        asof=DAY,
        generations=generations,
    )
    assert report["updated"] == ["economic-fred_csv"]
    assert [item["group"] for item in report["failed"]] == ["jquants-financial"]
    assert remote.values[input_key("jquants-financial")].body == previous
    target = tmp_path / "only-fred.sqlite"
    result = load_input(remote, "economic-fred_csv", target, tmp_path / "input")
    assert result["canonical_generations"] == generations
    with closing(initialize_database(target)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 0
