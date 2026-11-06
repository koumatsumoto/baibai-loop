"""H.15 planning inputs retain range receipts through both public transports."""

import json
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from tests.helpers.provider_fixtures import _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.acquisition.economic_staging as staging
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
from baibai_engine.data_collection.transport.context import FetchContext, ProviderFetchStoppedError
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = require_source("fed-h15")
GROUP = "economic-frb_h15"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 1, tzinfo=UTC)
PREAMBLE = ['"Release","Synthetic H.15 fixture"', "  Units: percent  "]
HEADER = "\n".join([*PREAMBLE, '"Time Period",RIFLGFCY10_N.B,RIFLGFCY02_N.B,OTHER']) + "\n"


def state(path):
    with closing(store.open_read_only_connection(path)) as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in (*store.RECEIPT_KEYS, *store.CONFIRMATION_TABLES)
        }


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(clock=WHEN, value="1.0", browser=False, requests=[], navigations=[])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def body():
        if isinstance(control.value, Exception):
            raise control.value
        row = "" if control.value is None else f'{DAY},{control.value},.,"literal, note"\n'
        return (HEADER + row).encode()

    class Session:
        def get(self, url, **kwargs):
            assert url == SOURCE.source_url
            control.requests.append(kwargs["params"])
            return _FakeResponse(b"" if control.browser else body())

        def close(self):
            pass

    class Browser:
        def fetch_download(self, url, **kwargs):
            control.navigations.append(dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)))
            return body()

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            return Browser()

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", Context)
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


def acquire(directory, canonical, mode, source, monkeypatch):
    remote = MemoryR2Store()
    with monkeypatch.context() as patch:
        if mode == "full":
            # Reproduce an old generation-bound envelope without changing its reader.
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
            patch.delitem(staging._PLANNING_INPUT_FORMATS, GROUP)
        assert inputs.publish_inputs(
            remote,
            market=directory / "unused",
            economic=canonical,
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
    source.requests.clear()
    source.navigations.clear()
    packet = directory / "packet"
    status = handoff.acquire("frb_h15", SOURCE.source_id, DAY, packet, database)
    assert len(source.requests) == 1
    assert source.requests[0]["from"] == recent_start(SOURCE, DAY).strftime("%m/%d/%Y")
    assert source.requests[0]["to"] == DAY.strftime("%m/%d/%Y")
    assert source.navigations == (source.requests if source.browser else [])
    handoff.validate_handoff(packet, "frb_h15", SOURCE.source_id)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='economic_frb_h15_input_days'"
            ).fetchone()
            is None
        )
    return raw, status, json.loads((packet / "result.json").read_text())


def integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


@pytest.mark.parametrize("browser", [False, True])
def test_plain_and_browser_rechecks_keep_changes_empty_failure_and_next_input(
    tmp_path, source, monkeypatch, browser
):
    original = tmp_path / "seed.sqlite"
    seed(original)
    original_bytes = original.read_bytes()
    source.browser = browser
    results = []
    for mode in ("full", "planning"):
        canonical = tmp_path / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original_bytes)
        phases = []
        for hour, value in enumerate(
            ("1.5", "1.0", None, ProviderFetchStoppedError("synthetic cutoff"), "ND"), 1
        ):
            source.clock, source.value = WHEN + timedelta(hours=hour), value
            before = state(canonical)
            with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
                directory = Path(temporary)
                raw, status, report = acquire(directory, canonical, mode, source, monkeypatch)
                failed = isinstance(value, Exception)
                assert status == int(failed)
                assert report["sources"][0]["status"] == ("failed" if failed else "ok")
                raw_bytes = raw.read_bytes()
                result = integrate(raw, canonical, directory / "accepted.sqlite")
                assert not result["late_recovery_required"]
                assert raw.read_bytes() == raw_bytes
                phases.append((state(raw), state(canonical), report, result))
            after = state(canonical)
            assert set(before["economic_source_records"]) <= set(after["economic_source_records"])
            if failed:
                assert after["economic_source_records"] == before["economic_source_records"]
            else:
                with closing(store.open_read_only_connection(canonical)) as connection:
                    received = next(
                        read_snapshots(connection, DAY, DAY, source_id=SOURCE.source_id)
                    )
                assert received.records == (
                    ()
                    if value is None
                    else (
                        {
                            "row": {
                                "Time Period": str(DAY),
                                "RIFLGFCY10_N.B": value,
                                "RIFLGFCY02_N.B": ".",
                                "OTHER": "literal, note",
                            },
                            "preamble": PREAMBLE,
                        },
                    )
                )
        results.append(phases)
    assert results[0] == results[1]
    assert original.read_bytes() == original_bytes
    changes = [row for row in state(canonical)["economic_source_records"] if row[1] == str(DAY)]
    assert [row[2] for row in changes] == [
        (WHEN + timedelta(hours=hour)).isoformat(timespec="microseconds")
        for hour in (0, 1, 2, 3, 5)
    ]


@pytest.mark.parametrize("hour", [1, 2])
def test_late_or_same_clock_response_keeps_newer_canonical(tmp_path, source, monkeypatch, hour):
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
