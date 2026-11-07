"""MOF planning seeds preserve actual CSV receipts and canonical acceptance."""

import json
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.mof.jgb as mof
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import recent_start
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = require_source("mof-jgbcm-all")
GROUP = "economic-mof_jgb"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 1, tzinfo=UTC)
HEADER = "synthetic rates,unit percent\n基準日,2年,10年\n"


def state(path):
    with closing(store.open_read_only_connection(path)) as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in (*store.RECEIPT_KEYS, *store.CONFIRMATION_TABLES)
        }


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(clock=WHEN, value="1.0", requests=[])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def fetch(_session, url, **kwargs):
        control.requests.append((url, kwargs.get("params")))
        if url == SOURCE.source_url:
            return (HEADER + "R8.11.5,2.0,3.0\n").encode("cp932")
        if isinstance(control.value, Exception):
            raise control.value
        row = "" if control.value is None else f"R8.11.6,{control.value},2.1\n"
        return (HEADER + row).encode("cp932")

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(mof, "fetch_bytes", fetch)
    monkeypatch.setattr(
        store.uuid, "uuid4", lambda: SimpleNamespace(hex=f"run{int(control.clock.timestamp())}")
    )
    return control


def seed(path):
    with closing(store.initialize_database(path)):
        pass
    outcome = acquisition.refresh_sources(
        [SOURCE.source_id], db_path=path, start=recent_start(SOURCE, DAY), end=DAY
    )
    assert isinstance(outcome[0], acquisition.SourceRefreshSuccess)


def acquire(directory, seed, mode, source, monkeypatch):
    remote = MemoryR2Store()
    with monkeypatch.context() as patch:
        if mode == "full":
            # The old generation-bound input still uses the same reader.
            patch.setattr(
                inputs,
                "export_day_input",
                lambda original, target, **_: export_receipts(
                    original,
                    target,
                    source_ids=[SOURCE.source_id],
                    since=datetime.min.replace(tzinfo=UTC),
                ),
            )
            # An old full input has no planning format discriminator.
            import baibai_jobs.acquisition.economic_staging as staging

            patch.delitem(staging._PLANNING_INPUT_FORMATS, GROUP)
        assert inputs.publish_inputs(
            remote,
            market=directory / "unused",
            economic=seed,
            identity="fixed",
            asof=DAY,
            generations={"economic_etag": "fixed-seed"},
            only_group=GROUP,
        ) == {"updated": [GROUP], "failed": []}
    database = directory / "input.sqlite"
    metadata = inputs.load_input(remote, GROUP, database, directory / "loaded")
    assert metadata["canonical_generations"] == {"economic_etag": "fixed-seed"}
    if mode == "planning":
        assert not state(database)["economic_source_records"]
    count = len(source.requests)
    packet = directory / "packet"
    status = handoff.acquire("mof_jgb", SOURCE.source_id, DAY, packet, database)
    assert source.requests[count:] == [
        (SOURCE.source_url, None),
        (mof._current_month_csv_url(SOURCE.source_url), None),
    ]
    handoff.validate_handoff(packet, "mof_jgb", SOURCE.source_id)
    with closing(store.open_read_only_connection(packet / "receipts.sqlite")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='economic_mof_jgb_input_days'"
            ).fetchone()
            is None
        )
    return packet / "receipts.sqlite", status, json.loads((packet / "result.json").read_text())


def integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_real_csv_rechecks_keep_a_b_a_disappearance_and_failed_current_file(
    tmp_path, source, monkeypatch
):
    original = tmp_path / "seed.sqlite"
    seed(original)
    original_bytes = original.read_bytes()
    final = []
    for mode in ("full", "planning"):
        canonical = tmp_path / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original_bytes)
        for hour, value in enumerate(
            ("1.5", "1.0", None, requests.RequestException("current file unavailable"), "-"), 1
        ):
            source.clock = WHEN + timedelta(hours=hour)
            source.value = value
            before = state(canonical)
            with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
                directory = Path(temporary)
                raw, status, report = acquire(directory, original, mode, source, monkeypatch)
                failed = isinstance(value, Exception)
                assert status == int(failed)
                assert report["sources"][0]["status"] == ("failed" if failed else "ok")
                raw_bytes = raw.read_bytes()
                result = integrate(raw, canonical, directory / "accepted.sqlite")
                assert not result["late_recovery_required"]
                assert raw.read_bytes() == raw_bytes
            after = state(canonical)
            assert set(before["economic_source_records"]) <= set(after["economic_source_records"])
            if failed:
                assert after["economic_source_records"] == before["economic_source_records"]
            if value is None:
                with closing(store.open_read_only_connection(canonical)) as connection:
                    assert (
                        next(
                            read_snapshots(connection, DAY, DAY, source_id=SOURCE.source_id)
                        ).records
                        == ()
                    )
        final.append(state(canonical))
    assert final[0] == final[1]
    assert original.read_bytes() == original_bytes
    changes = [row for row in final[0]["economic_source_records"] if row[1] == str(DAY)]
    assert [row[2] for row in changes] == [
        (WHEN + timedelta(hours=hour)).isoformat(timespec="microseconds")
        for hour in (0, 1, 2, 3, 5)
    ]


@pytest.mark.parametrize("hour", [1, 2])
def test_late_and_same_clock_receipts_do_not_replace_newer_canonical(
    tmp_path, source, monkeypatch, hour
):
    original = tmp_path / "seed.sqlite"
    seed(original)
    original_bytes = original.read_bytes()
    for mode in ("full", "planning"):
        canonical = tmp_path / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original_bytes)
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            directory = Path(temporary)
            source.clock, source.value = WHEN + timedelta(hours=2), "1.5"
            raw, status, _ = acquire(directory, original, mode, source, monkeypatch)
            assert status == 0
            integrate(raw, canonical, directory / "accepted.sqlite")
        before = state(canonical)
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            directory = Path(temporary)
            source.clock, source.value = WHEN + timedelta(hours=hour), "1.9"
            raw, status, _ = acquire(directory, original, mode, source, monkeypatch)
            assert status == 0
            raw_bytes = raw.read_bytes()
            if hour == 2:
                with pytest.raises(EconomicReceiptConflict):
                    integrate(raw, canonical, directory / "accepted.sqlite")
            else:
                result = integrate(raw, canonical, directory / "accepted.sqlite")
                assert result["late_recovery_required"]
            assert raw.read_bytes() == raw_bytes
        assert state(canonical)["economic_source_records"] == before["economic_source_records"]
    assert original.read_bytes() == original_bytes
