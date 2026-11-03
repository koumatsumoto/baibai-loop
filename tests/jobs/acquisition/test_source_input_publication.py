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

    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_market_source_input", fail_market_input
    )
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


def test_read_scoped_input_from_same_group_previous_stage(tmp_path, monkeypatch):
    import json

    import pytest

    from baibai_engine.storage.objects.r2 import LakePublishError

    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(b"fixed content"),
    )
    publish_inputs(
        remote,
        market=tmp_path / "market",
        economic=tmp_path / "economic",
        asof=DAY,
        identity="first",
        generations={"version": "one"},
    )
    first = "coordination/source-inputs/economic-fred_csv-first/handoff.json"
    value = json.loads(remote.values[first].body)
    value["result"]["canonical_generations"] = {"version": "two"}
    second = "coordination/source-inputs/economic-fred_csv-second/handoff.json"
    remote.store_object(second, json.dumps(value).encode())
    remote.store_object(
        input_key("economic-fred_csv"),
        json.dumps(
            {"stage_id": "economic-fred_csv-second", "canonical_generations": {"version": "two"}}
        ).encode(),
    )
    result = load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "read")
    assert result["canonical_generations"] == {"version": "two"}
    assert (tmp_path / "db").read_bytes() == b"fixed content"
    value["receipt_key"] = value["receipt_key"].replace("economic-fred_csv-", "other-")
    remote.store_object(second, json.dumps(value).encode())
    with pytest.raises(LakePublishError, match="identity differs"):
        load_input(remote, "economic-fred_csv", tmp_path / "db2", tmp_path / "read2")


def test_unchanged_input_reuses_receipt_and_refreshes_generation(tmp_path, monkeypatch):
    import json

    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(b"identical database"),
    )
    args = dict(market=tmp_path / "market", economic=tmp_path / "economic", asof=DAY)
    for identity in ("first", "second"):
        result = publish_inputs(
            remote, **args, identity=identity, generations={"version": identity}
        )
        assert result["failed"] == []
    receipts = [key for key in remote.values if key.endswith(".sqlite")]
    assert len(receipts) == 1
    pointer = json.loads(remote.values[input_key("economic-fred_csv")].body)
    assert pointer["stage_id"] == "economic-fred_csv-second"
    result = load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "retrieved")
    assert result["canonical_generations"] == {"version": "second"}
    assert (tmp_path / "db").read_bytes() == b"identical database"


def test_changed_input_creates_new_receipt(tmp_path, monkeypatch):
    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    source = tmp_path / "input-data"
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(source.read_bytes()),
    )
    args = dict(market=tmp_path / "market", economic=tmp_path / "economic", asof=DAY)
    for identity in ("first", "second"):
        source.write_bytes(identity.encode())
        result = publish_inputs(
            remote, **args, identity=identity, generations={"version": identity}
        )
        assert result["failed"] == []
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "retrieved")
    assert (tmp_path / "db").read_bytes() == b"second"


def test_missing_previous_blob_is_recreated_under_new_stage(tmp_path, monkeypatch):
    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(b"same"),
    )
    args = dict(market=tmp_path / "market", economic=tmp_path / "economic", asof=DAY)
    publish_inputs(remote, **args, identity="first", generations={"version": "one"})
    old_key = next(key for key in remote.values if key.endswith(".sqlite"))
    del remote.values[old_key]
    result = publish_inputs(remote, **args, identity="second", generations={"version": "two"})
    assert result["failed"] == []
    assert old_key not in remote.values
    assert any("fred_csv-second/" in key for key in remote.values if key.endswith(".sqlite"))
    load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "retrieved")
    assert (tmp_path / "db").read_bytes() == b"same"


def test_reused_input_refuses_a_receipt_from_another_group(tmp_path, monkeypatch):
    import json

    import pytest

    from baibai_engine.storage.objects.r2 import LakePublishError

    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(b"same"),
    )
    args = dict(market=tmp_path / "market", economic=tmp_path / "economic", asof=DAY)
    for identity in ("first", "second"):
        publish_inputs(remote, **args, identity=identity, generations={"version": identity})
    manifest_key = "coordination/source-inputs/economic-fred_csv-second/handoff.json"
    manifest = json.loads(remote.values[manifest_key].body)
    manifest["receipt_key"] = manifest["receipt_key"].replace("economic-fred_csv-", "other-")
    remote.store_object(manifest_key, json.dumps(manifest).encode())
    with pytest.raises(LakePublishError, match="identity differs"):
        load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "retrieved")
    assert not (tmp_path / "db").exists()


def test_invalid_previous_blob_is_not_reused(tmp_path, monkeypatch):
    remote = MemoryR2Store()
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.economic_acquisition_groups", lambda: ("fred_csv",)
    )
    monkeypatch.setattr("baibai_jobs.acquisition.source_inputs.SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_economic_receipts",
        lambda _source, target, **_kwargs: target.write_bytes(b"same"),
    )
    args = dict(market=tmp_path / "market", economic=tmp_path / "economic", asof=DAY)
    publish_inputs(remote, **args, identity="first", generations={"version": "one"})
    old_key = next(key for key in remote.values if key.endswith(".sqlite"))
    remote.values[old_key].metadata["sha256"] = "bad"
    result = publish_inputs(remote, **args, identity="second", generations={"version": "two"})
    assert result["failed"] == []
    load_input(remote, "economic-fred_csv", tmp_path / "db", tmp_path / "retrieved")
    assert (tmp_path / "db").read_bytes() == b"same"
    assert any("fred_csv-second/" in key for key in remote.values if key.endswith(".sqlite"))
