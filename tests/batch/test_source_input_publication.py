from contextlib import closing

from tests.engine.test_tradingview_collector import DAY, store as market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_batch.storage.source_inputs import input_key, publish_inputs, load_input
from baibai_engine.collection.economic.store import initialize_database
from baibai_engine.storage.observations.schema import open_connection


def test_bad_tradingview_calendar_retains_prior_input_and_still_updates_economic_groups(
    tmp_path, monkeypatch
):
    remote = MemoryR2Store()
    market = market_store(tmp_path / "market.sqlite", 2)
    economic = tmp_path / "economic.sqlite"
    with closing(initialize_database(economic)):
        pass
    monkeypatch.setattr("baibai_batch.storage.source_inputs.SOURCE_INPUT_GROUPS", ("tradingview",))
    monkeypatch.setattr(
        "baibai_batch.storage.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
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
    previous = remote.values[input_key("tradingview")].body
    with closing(open_connection(market)) as conn, conn:
        conn.execute("DELETE FROM jquants_market_calendar")
    report = publish_inputs(
        remote,
        market=market,
        economic=economic,
        identity="second",
        asof=DAY,
        generations=generations,
    )
    assert report["updated"] == ["economic-fred_csv"]
    assert [item["group"] for item in report["failed"]] == ["tradingview"]
    assert remote.values[input_key("tradingview")].body == previous
    target = tmp_path / "only-fred.sqlite"
    result = load_input(remote, "economic-fred_csv", target, tmp_path / "input")
    assert result["canonical_generations"] == generations
    with closing(initialize_database(target)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 0
