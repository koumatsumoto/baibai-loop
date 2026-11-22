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


def test_commit_work_reads_selected_shared_pack_once(tmp_path, monkeypatch, capsys):
    import baibai_jobs.acquisition.jquants_master_native as command
    from baibai_engine.data_collection.providers.jquants.master_native import (
        HEAD_KEY,
        PREFIX,
        MasterAttempt,
    )

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = MasterL1(store, tmp_path)
    owner.initialize()
    days = [DAY + timedelta(days=offset) for offset in range(5)]

    def attempt(identity, day, rows):
        pages = (
            tuple(Client(rows).pages("master", {"date": day.isoformat()}))
            if rows is not None
            else ()
        )
        return MasterAttempt(identity, day, AT, pages, None if pages else "interrupted")

    # A sparse saved directory must not consult the unrelated intervening pack.
    owner.commit([attempt("unrelated", days[1], [{"Code": "13320"}])])
    unrelated = next(key for key in store.values if key.startswith(PREFIX + "packs/"))
    originals = [
        attempt("current", days[0], [{"Code": "13010"}]),
        attempt("empty", days[2], []),
        attempt("failed", days[3], None),
        attempt("adopted", days[4], [{"Code": "13010"}]),
    ]
    owner.commit(originals)
    pack = next(
        key for key in store.values if key.startswith(PREFIX + "packs/") and key != unrelated
    )
    held = attempt("held", days[4], [{"Code": "13020"}])
    assert owner.commit([held])["held"] == ["held"]
    for item in [*originals, held]:
        (tmp_path / f"attempt-{item.request_id}.json").write_text(json.dumps(item.value()))
    fixed = owner.fixed()
    writes = store.put_keys.copy()
    get = store.get_bytes_with_metadata

    def read_selected(key):
        assert key != unrelated
        return get(key)

    monkeypatch.setattr(store, "get_bytes_with_metadata", read_selected)
    monkeypatch.setattr(command, "Boto3R2Store", lambda **_: store)
    store.get_keys.clear()
    args = ["commit-work", "--directory", str(tmp_path)]
    assert command.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "changed": [],
        "equal": [],
        "failed": [],
        "held": [],
        "retried": ["adopted", "current", "empty", "failed", "held"],
        "unresolved": {
            days[offset].isoformat(): ["nonempty_master_required"] for offset in (2, 3, 4)
        },
    }
    assert [key for key in store.get_keys if key.startswith(PREFIX + "packs/")] == [pack]
    assert store.put_keys == writes
    assert owner.fixed().payload == fixed.payload

    # Both pack members and their fingerprints still verify, but neither one
    # fulfills a scope that points to the other date's request.
    swapped = fixed.value()
    first, last = (swapped["scopes"][days[offset].isoformat()] for offset in (0, 4))
    first["adopted"], last["adopted"] = last["adopted"], first["adopted"]
    store.store_object(HEAD_KEY, json.dumps(swapped).encode())
    assert command.main(args) == 1
    assert json.loads(capsys.readouterr().out)["unresolved"] == {
        **report["unresolved"],
        days[0].isoformat(): ["nonempty_master_required"],
    }
    assert store.put_keys == writes
    store.store_object(HEAD_KEY, fixed.payload)

    # A missing selected pack is still an integrity failure, not an empty result.
    def missing_selected(key):
        return None if key == pack else read_selected(key)

    monkeypatch.setattr(store, "get_bytes_with_metadata", missing_selected)
    assert command.main(args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "LakePublishError" in output.err
    assert store.put_keys == writes
    assert owner.fixed().payload == fixed.payload
