"""Normal generation fixes native economic membership, without a daily DB copy."""

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.financial_native import seed_empty_financial
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master import insert_master_rows
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.price_native import seed_prices
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview_market import CLOCK, capture

import baibai_jobs.integration.models as job
import baibai_jobs.publication.snapshot_publication as publication
from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.economic_inputs import (
    rebuild_economic_input,
    retain_economic_input,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.screening.discovery_contract import DiscoverySelection
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_jobs.cli import main as jobs_cli
from baibai_jobs.publication.datasets import LAKE_DATASETS

DAY = date(2026, 1, 5)


def seed(path, *, value="100000", at=CLOCK):
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=at,
            records_by_day={date(2026, 1, 1): [{"PAYEMS": value}]},
        )


@pytest.fixture
def integration(tmp_path, monkeypatch):
    names = ("jquants.daily_bars", "jquants.master_snapshots", "jquants.market_calendar")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(job, "_load_handle", lambda _: None)
    monkeypatch.setattr(job, "verify_held", lambda *_: None)
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 100, 1),),
        short_sale_reports=(),
    )
    with closing(sqlite3.connect(market)) as connection, connection:
        insert_master_rows(
            connection, [(DAY.isoformat(), "1301", "合成企業", "プライム", "卸売業", "011")]
        )
    economic = tmp_path / "economic.sqlite"
    seed(economic)
    with closing(initialize_database(economic)) as connection:
        retain_market_capture(connection, capture())
    store = MemoryR2Store()
    seed_empty_financial(store, tmp_path / "financial-native", DAY)
    seed_prices(store, market, tmp_path / "native-price", as_of=DAY)
    seed_calendar(store, market, tmp_path / "native-calendar", as_of=DAY)
    seed_master(store, market, tmp_path / "native-master", as_of=DAY)
    return dict(
        store=store,
        edinet_input=empty_edinet_input(store, tmp_path),
        market=market,
        economic=economic,
        runs=tmp_path / "runs.sqlite",
        mirror=tmp_path / "mirror",
        as_of=DAY,
        lease_handle=tmp_path / "lease",
        growth_start=None,
    )


def test_daily_cli_generates_without_articles_and_retains_economic_input(
    integration, tmp_path, monkeypatch
):
    args = integration
    original = args["economic"].read_bytes()
    # The CLI consumes an existing Machine pair; publish a real synthetic predecessor.
    prior = job.integrate_models(**args)
    assert prior["models_ready"] is True

    class FixtureTransport:
        def __new__(cls, *, bucket, env=None, verify_put_with_head=True):
            return args["store"]

    monkeypatch.setattr(job, "Boto3R2Store", FixtureTransport)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/baibai-loop")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "example/baibai-loop/.github/workflows/cloud-daily-batch-current.yml@refs/heads/main",
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"asof": DAY.isoformat(), "edinet_input": args["edinet_input"].model_dump()})
    )
    notice = tmp_path / "notice.json"
    assert (
        jobs_cli(
            [
                "model-integration",
                "--asof",
                DAY.isoformat(),
                "--market",
                str(args["market"]),
                "--economic",
                str(args["economic"]),
                "--runs",
                str(args["runs"]),
                "--mirror",
                str(args["mirror"]),
                "--lease-handle",
                str(args["lease_handle"]),
                "--integration-report",
                str(report),
                "--notice-output",
                str(notice),
                "--daily-origin",
            ]
        )
        == 0
    )
    result = json.loads(notice.read_bytes())
    assert result["models_ready"] is True
    assert result["daily_run"] == "1-1"
    assert args["economic"].read_bytes() == original
    saved = read_generation(result["generation_id"], path=args["runs"])
    assert saved is not None
    assert saved.generation_id != prior["generation_id"]
    assert read_publication(path=args["runs"], selection_id=prior["selection_id"]) is not None
    machine = job.read_machine_current(args["store"]).pointer
    assert machine is not None
    assert (saved.inputs["market"].reference, saved.inputs["market"].sha256) == (
        machine.release_id,
        machine.manifest_sha256,
    )
    assert saved.subject_plan is not None
    assert {k for k, plan in saved.subject_plan.items() if plan.scope != "stock"} == {"market"}
    current = read_publication(path=args["runs"])
    assert current.selection_id == result["selection_id"]
    discovery = current.selection.result
    assert isinstance(discovery, DiscoverySelection)
    row = next(row for row in discovery.rows if row["ticker"] == "1301")
    assert (row["price"], row["price_date"]) == (100, DAY.isoformat())
    identity = saved.inputs["economic"]
    assert identity.kind == "native_l1"
    assert identity.sha256 == result["economic_input_sha256"]
    actual = saved.result("market_context", "market").payload
    expected = {key: actual[key] for key in ("reading", "tradingview_market")}
    payrolls = next(
        row for row in actual["reading"]["series"] if row["series_id"] == "us.nonfarm_payrolls"
    )
    assert (payrolls["latest_value"], payrolls["observed_at"]) == (100000, "2026-01-01")
    assert payrolls["vintage_at"] == CLOCK.isoformat()
    cold = rebuild_economic_input(args["store"], args["economic"], identity)
    assert (cold.as_of, cold.evaluation_time) == (saved.as_of, saved.evaluation_time)
    assert cold.context == expected
    objects = {
        key: value
        for key, value in args["store"].values.items()
        if key.startswith("fixed-inputs/economic/")
    }
    assert objects
    assert all(item.content_type == "application/json" for item in objects.values())
    assert all(b'PAYEMS": "100000"' not in item.body for item in objects.values())
    root = json.loads(
        objects[f"fixed-inputs/economic/v1/{identity.descriptor['selection_sha256']}.json"].body
    )
    assert root["as_of"] == saved.as_of.isoformat()
    assert root["evaluation_time"] == saved.evaluation_time.isoformat().replace("+00:00", "Z")

    seed(args["economic"], value="200000", at=CLOCK + timedelta(minutes=2))
    with closing(initialize_database(args["economic"])) as connection:
        retain_market_capture(
            connection,
            capture(receipt_id="failed-later", error=True, received=CLOCK + timedelta(minutes=3)),
        )
    later = retain_economic_input(
        args["store"], args["economic"], as_of=saved.as_of, evaluation_time=saved.evaluation_time
    )
    assert rebuild_economic_input(args["store"], args["economic"], later).context != expected
    args["runs"].unlink()  # L2 results are not an input to the cold calculation.
    after = args["economic"].read_bytes()
    assert rebuild_economic_input(args["store"], args["economic"], identity).context == expected
    assert args["economic"].read_bytes() == after


@pytest.mark.parametrize("failure", ["metadata", "original"])
def test_economic_fixed_input_failure_does_not_publish_generation_or_lake(
    integration, monkeypatch, failure
):
    args = integration
    if failure == "metadata":
        put = args["store"].put_file

        def fail_metadata(key, *values, **kwargs):
            if key.startswith("fixed-inputs/economic/"):
                raise LakePublishError("fixed metadata not saved")
            return put(key, *values, **kwargs)

        monkeypatch.setattr(args["store"], "put_file", fail_metadata)
        error = LakePublishError
    else:
        retain = job.EconomicInputSession.retain

        def remove_original(*values, **kwargs):
            saved = retain(*values, **kwargs)
            with closing(sqlite3.connect(args["economic"])) as connection, connection:
                connection.execute("DELETE FROM economic_source_records")
            return saved

        monkeypatch.setattr(job.EconomicInputSession, "retain", remove_original)
        error = ValueError
    with pytest.raises(error):
        job.integrate_models(**args)
    assert not args["runs"].exists()
    assert current_release_pointer_key() not in args["store"].values
