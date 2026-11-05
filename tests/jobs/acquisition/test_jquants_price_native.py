"""Independent daily price acquisition survives unrelated projection/publication failures."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_engine.data_collection.providers.jquants.price_native import PREFIX, PriceL1
from baibai_jobs.acquisition.jquants_price_native import acquire_price_native

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


class Client:
    def __init__(self, at=AT, *, close=100, partial=False, no_pages=False):
        self.at, self.close_value, self.partial, self.no_pages = at, close, partial, no_pages
        self.calls = []

    def close(self):
        pass

    def pages(self, source, params):
        self.calls.append((source, params))
        if not self.no_pages:
            yield JQuantsPage(
                source,
                params,
                self.at,
                json.dumps(
                    {
                        "data": [{"Code": "13010", "Date": DAY.isoformat(), "C": self.close_value}],
                        **({"pagination_key": "next"} if self.partial else {}),
                    }
                ).encode(),
            )
        if self.partial or self.no_pages:
            raise JQuantsAcquisitionError("provider interrupted")


def test_price_acquires_equal_and_corrected_cycles_without_any_l2(tmp_path):
    store = MemoryR2Store()
    PriceL1(store, tmp_path / "init").initialize()
    snapshots = []
    for count, value in enumerate((100, 100, 110)):
        clock = AT + timedelta(hours=count)
        owner = PriceL1(store, tmp_path / str(count))
        report = acquire_price_native(
            owner,
            start=DAY,
            end=DAY,
            revision_overlap_days=7,
            client=Client(clock, close=value),
            clock=lambda clock=clock: clock,
        )
        assert report["equal" if count == 1 else "changed"] == 1
        snapshots.append(owner.fixed())
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == 2
    assert next(snapshots[0].requests(store, days=[DAY])).request.records()[0]["C"] == 100
    assert next(snapshots[2].requests(store, days=[DAY])).request.records()[0]["C"] == 110


@pytest.mark.parametrize("no_pages", [False, True])
def test_price_partial_or_no_page_failure_is_durable_and_recoverable(tmp_path, no_pages):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "work")
    owner.initialize()
    report = acquire_price_native(
        owner,
        start=DAY,
        end=DAY,
        revision_overlap_days=0,
        client=Client(partial=not no_pages, no_pages=no_pages),
        clock=lambda: AT,
    )
    assert report["failed"] == 1
    failed = owner.fixed()
    assert next(failed.requests(store, days=[DAY])).error is not None
    fresh = PriceL1(store, tmp_path / "next")
    later = AT + timedelta(hours=1)
    assert (
        acquire_price_native(
            fresh,
            start=DAY,
            end=DAY,
            revision_overlap_days=0,
            client=Client(later),
            clock=lambda: later,
        )["changed"]
        == 1
    )
    assert failed.value()["scopes"][DAY.isoformat()]["failure"]
    assert fresh.fixed().value()["scopes"][DAY.isoformat()]["failure"] is None


def test_price_commits_before_an_unavailable_legacy_market_release(tmp_path, monkeypatch):
    import baibai_engine.data_collection.providers.jquants.http as http
    import baibai_engine.primitives.market_windows as windows
    import baibai_engine.storage.snapshots.objects as objects
    import baibai_jobs.integration.source_handoff as handoff

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    PriceL1(store, tmp_path / "init").initialize()
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    monkeypatch.setattr(windows, "FINANCIAL_HISTORY_DAYS", 0)
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda _: Client())

    def fail_legacy(**_):
        raise ValueError("legacy market release unavailable")

    monkeypatch.setattr(objects, "open_lake", fail_legacy)
    with pytest.raises(ValueError, match="legacy market release unavailable"):
        handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    fixed = PriceL1(store, tmp_path / "cold-reader").fixed()
    assert next(fixed.requests(store, days=[DAY])).request.records()[0]["C"] == 100
    assert (
        json.loads((tmp_path / "acquire" / "price-native-result.json").read_bytes())["changed"] == 1
    )


def test_final_rate_limit_halts_shared_quota_after_preserving_price_failure(tmp_path):
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRateLimitError

    class LimitedClient(Client):
        def pages(self, source, params):
            raise JQuantsRateLimitError("quota exhausted")
            yield

    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path)
    owner.initialize()
    report = acquire_price_native(
        owner, start=DAY, end=DAY, revision_overlap_days=0, client=LimitedClient(), clock=lambda: AT
    )
    assert report["failed"] == report["halted"] == 1
    assert next(owner.fixed().requests(store, days=[DAY])).error == "JQuantsRateLimitError"


def test_price_cli_acquisition_replay_and_fixed_head_rebuild(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    from baibai_jobs import cli
    from baibai_jobs.acquisition import jquants_price_native as acquisition
    from baibai_jobs.integration import price_native as integration

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(acquisition, "JQuantsHTTP", lambda _: Client())
    work = str(tmp_path / "work")
    assert cli.main(["jquants-price-native", "initialize", "--directory", work]) == 0
    assert (
        cli.main(
            [
                "jquants-price-native",
                "acquire",
                "--directory",
                work,
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert cli.main(["jquants-price-native", "commit-work", "--directory", work]) == 0
    assert (
        cli.main(
            [
                "rebuild-price-native",
                "--work-directory",
                str(tmp_path / "read"),
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    assert pq.read_table(tmp_path / "output" / "daily_bars.parquet").to_pylist()[0]["close"] == 100
    assert (tmp_path / "output" / "head.json").read_bytes() == PriceL1(
        store, tmp_path / "cold"
    ).fixed().payload


@pytest.mark.parametrize("conflict", [False, True])
def test_native_storage_or_writer_failure_stops_before_legacy_acquisition(
    tmp_path, monkeypatch, conflict
):
    import baibai_engine.data_collection.providers.jquants.http as http
    import baibai_engine.storage.snapshots.objects as objects
    import baibai_jobs.acquisition.jquants_price_native as acquisition
    import baibai_jobs.integration.source_handoff as handoff
    from baibai_engine.storage.objects.r2 import LakeCASConflict, R2Error

    class Store(MemoryR2Store):
        def close(self):
            pass

    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: Store())
    monkeypatch.setattr(http, "JQuantsHTTP", lambda _: Client())
    failure = LakeCASConflict if conflict else R2Error

    def failed(*args, **kwargs):
        raise failure("storage failed")

    monkeypatch.setattr(acquisition, "acquire_price_native", failed)
    monkeypatch.setattr(
        objects, "open_lake", lambda **_: pytest.fail("legacy started after global storage failure")
    )
    with pytest.raises(failure, match="storage failed"):
        handoff._acquire_market_l1(DAY, tmp_path / "work")
