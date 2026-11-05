"""Master raw receipt completion is independent of downstream snapshot usability."""

import json
from datetime import UTC, date, datetime, timedelta

from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
from baibai_jobs.acquisition.jquants_master_native import acquire_master_native

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, tzinfo=UTC)


class Client:
    def __init__(self, records, at=AT):
        self.records, self.at, self.calls = records, at, []

    def pages(self, source, params):
        self.calls.append((source, params))
        yield JQuantsPage(source, params, self.at, json.dumps({"data": self.records}).encode())


def test_empty_then_nonempty_receipt_retries_without_l2_and_stops_when_fulfilled(tmp_path):
    store = MemoryR2Store()
    MasterL1(store, tmp_path / "init").initialize()
    empty = Client([])
    first = acquire_master_native(
        MasterL1(store, tmp_path / "empty"), start=DAY, end=DAY, client=empty, clock=lambda: AT
    )
    assert first["changed"] == first["incomplete"] == 1
    nonempty = Client([{"Code": "13010"}], AT + timedelta(hours=1))
    second = acquire_master_native(
        MasterL1(store, tmp_path / "nonempty"),
        start=DAY,
        end=DAY,
        client=nonempty,
        clock=lambda: AT + timedelta(hours=1),
    )
    assert second["changed"] == 1
    assert second["incomplete"] == 0
    unused = Client([])
    third = acquire_master_native(
        MasterL1(store, tmp_path / "fulfilled"), start=DAY, end=DAY, client=unused
    )
    assert third["changed"] == third["incomplete"] == 0
    assert unused.calls == []


def test_explicit_force_rechecks_same_day_for_tradingview_backfill(tmp_path):
    store = MemoryR2Store()
    MasterL1(store, tmp_path / "init").initialize()
    for index, force in enumerate((False, True)):
        at = AT + timedelta(hours=index)
        client = Client([{"Code": f"130{index}0"}], at)
        result = acquire_master_native(
            MasterL1(store, tmp_path / str(index)),
            start=DAY,
            end=DAY,
            client=client,
            force=force,
            clock=lambda at=at: at,
        )
        assert result["changed"] == 1
        assert len(client.calls) == 1


def test_commit_work_does_not_turn_saved_empty_into_fulfilled_master(tmp_path, monkeypatch, capsys):
    import baibai_jobs.acquisition.jquants_master_native as command

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = MasterL1(store, tmp_path)
    owner.initialize()
    acquire_master_native(owner, start=DAY, end=DAY, client=Client([]), clock=lambda: AT)
    monkeypatch.setattr(command, "Boto3R2Store", lambda **_: store)
    assert command.main(["commit-work", "--directory", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["unresolved"] == {DAY.isoformat(): ["nonempty_master_required"]}
    assert len(result["retried"]) == 1
