"""Scoped native receipts keep their actual clock without a full-source proof."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.provider_fixtures import _FakeResponse

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.data_collection.transport.context import FetchContext
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_jobs.integration.native_source_merge import (
    merge_stores,
    reconcile_stores,
    verify_stores,
)

SOURCE = "nikkei-pbr"
DAY = date(2026, 11, 6)
STAMP = datetime(2026, 11, 6, 18, tzinfo=UTC)


def receive(path, value, hour, *, day=DAY, source_id=SOURCE):
    with closing(initialize_database(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id=source_id,
            observed_at=day,
            fetched_at=STAMP + timedelta(hours=hour),
            records=[] if value is None else [{"weighted_text": value, "index_based_text": "2.80"}],
        )


def apply(raw, canonical, output):
    original = raw.read_bytes()
    report = accept_rechecks(raw, canonical, output)
    merge_stores(output, canonical)
    verify_stores(output, canonical)
    assert raw.read_bytes() == original
    return report


def current(path, *, day=DAY, source_id=SOURCE):
    with closing(initialize_database(path)) as connection:
        validate_collection_confirmations(connection)
        state = connection.execute(
            "SELECT checked_at,adopted_fetched_at FROM economic_source_record_confirmations "
            "WHERE source_id=? AND observed_at=?",
            (source_id, str(day)),
        ).fetchone()
        row = next(read_snapshots(connection, day, day, source_id=source_id))
        assert not connection.execute(
            "SELECT 1 FROM economic_source_collection_confirmations WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return row, tuple(state) if state is not None else None


@pytest.mark.parametrize("value", ["2.01", None])
def test_unchanged_range_receipt_blocks_an_older_revision_and_same_clock_conflict(tmp_path, value):
    canonical, same = tmp_path / "canonical", tmp_path / "same"
    receive(canonical, value, 0)
    receive(same, value, 3)
    report = apply(same, canonical, tmp_path / "accepted")
    assert report["unchanged_rechecks"] == 1
    row, state = current(canonical)
    assert row.fetched_at == STAMP
    assert state == tuple(
        (STAMP + timedelta(hours=h)).isoformat(timespec="microseconds") for h in (3, 0)
    )
    with closing(initialize_database(canonical)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
    before = canonical.read_bytes()
    late = tmp_path / "late"
    receive(late, "3.01", 2)
    assert apply(late, canonical, tmp_path / "late-accepted")["late_recovery_required"]
    assert canonical.read_bytes() == before
    conflict = tmp_path / "conflict"
    receive(conflict, "3.01", 3)
    with pytest.raises(EconomicReceiptConflict, match="same received time"):
        accept_rechecks(conflict, canonical, tmp_path / "conflict-accepted")
    assert canonical.read_bytes() == before
    apply(same, canonical, tmp_path / "replay")
    assert current(canonical) == (row, state)


def test_range_confirmation_export_keeps_anchor_and_other_periods_independent(tmp_path):
    canonical, same, exported = (tmp_path / name for name in ("canonical", "same", "exported"))
    receive(canonical, "A", 0)
    other = DAY - timedelta(days=1)
    receive(canonical, "untouched", 0, day=other)
    receive(same, "A", 3)
    apply(same, canonical, tmp_path / "accepted")
    export_receipts(canonical, exported, source_ids=[SOURCE], since=STAMP + timedelta(hours=3))
    assert current(exported) == current(canonical)
    with closing(initialize_database(exported)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
    earlier = tmp_path / "earlier-other"
    receive(earlier, "changed", 2, day=other)
    assert not apply(earlier, canonical, tmp_path / "other-accepted")["late_recovery_required"]
    assert current(canonical, day=other)[0].records[0]["weighted_text"] == "changed"


def test_range_a_b_a_and_disappearance_keep_actual_change_receipts(tmp_path):
    canonical = tmp_path / "canonical"
    receive(canonical, "A", 0)
    for hour, value in ((1, "A"), (2, "B"), (3, "A"), (4, None), (5, None), (6, "A")):
        raw = tmp_path / f"raw{hour}"
        receive(raw, value, hour)
        assert not apply(raw, canonical, tmp_path / f"accepted{hour}")["late_recovery_required"]
    with closing(initialize_database(canonical)) as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT fetched_at FROM economic_source_records ORDER BY fetched_at"
            )
        ] == [
            (STAMP + timedelta(hours=hour)).isoformat(timespec="microseconds")
            for hour in (0, 2, 3, 4, 6)
        ]
    assert current(canonical)[0].fetched_at == STAMP + timedelta(hours=6)


def test_legacy_exact_replay_does_not_rewind_confirmation_or_lose_raw(tmp_path):
    canonical, same, replay = (tmp_path / name for name in ("canonical", "same", "replay"))
    receive(canonical, "A", 0)
    receive(replay, "A", 0)
    receive(same, "A", 3)
    apply(same, canonical, tmp_path / "accepted")
    before = current(canonical)
    assert not apply(replay, canonical, tmp_path / "replayed")["late_recovery_required"]
    assert current(canonical) == before
    local = tmp_path / "local"
    receive(local, "B", 2)
    original = local.read_bytes()
    with pytest.raises(MergeError, match="explicit source recheck"):
        reconcile_stores(canonical, local)
    assert local.read_bytes() == original
    receive(local, "A", 4)
    reconcile_stores(canonical, local)
    assert current(local)[0].records[0]["weighted_text"] == "A"
    assert current(local)[1][0] == (STAMP + timedelta(hours=4)).isoformat(timespec="microseconds")
    with closing(initialize_database(local)) as connection:
        assert connection.execute(
            "SELECT 1 FROM economic_source_records WHERE fetched_at=?",
            ((STAMP + timedelta(hours=2)).isoformat(timespec="microseconds"),),
        ).fetchone()


@pytest.mark.parametrize("damage", ["missing", "digest", "clock"])
def test_range_confirmation_validation_checks_its_own_exact_anchor(tmp_path, damage):
    canonical, raw = tmp_path / "canonical", tmp_path / "raw"
    receive(canonical, "A", 0)
    receive(raw, "A", 3)
    apply(raw, canonical, tmp_path / "accepted")
    with closing(initialize_database(canonical)) as connection, connection:
        if damage == "missing":
            connection.execute("DELETE FROM economic_source_records")
        elif damage == "digest":
            connection.execute(
                "UPDATE economic_source_record_confirmations SET content_sha256='bad'"
            )
        else:
            connection.execute(
                "UPDATE economic_source_record_confirmations SET checked_at='1900-01-01'"
            )
        with pytest.raises(ValueError, match="scoped confirmation lost its period anchor"):
            validate_collection_confirmations(connection)


def test_http_acquisition_preserves_same_value_clock_and_partial_range_failure(
    tmp_path, monkeypatch
):
    control = SimpleNamespace(hour=0, value="2.01", fail=False, calls=[])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (STAMP + timedelta(hours=control.hour)).astimezone(tz or UTC)

    class Session:
        def get(self, url, **kwargs):
            params = kwargs["params"]
            assert params["type"] == "1"
            assert params["year"] == "2026"
            assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
            metric, month = params["list"], int(params["month"])
            control.calls.append((metric, month))
            if control.fail and metric == "pbr" and month == 11:
                return _FakeResponse(b"<html>incomplete page</html>")
            day = DAY if month == 11 else date(2026, 10, 1)
            return _FakeResponse(
                (
                    f"<table><tr><td>{day:%Y.%m.%d}</td><td>{control.value}</td>"
                    "<td>2.80</td></tr></table>"
                ).encode()
            )

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("Nikkei must use the monthly HTTP endpoint")

    monkeypatch.setattr(acquisition, "FetchContext", Context)
    for module in (acquisition, handoff):
        monkeypatch.setattr(module, "datetime", Clock)
    canonical = tmp_path / "canonical"
    with closing(initialize_database(canonical)):
        pass
    seed = canonical.read_bytes()
    for hour, value in ((0, "2.01"), (3, "2.01"), (2, "3.01")):
        control.hour, control.value, control.calls = hour, value, []
        database, packet = tmp_path / f"input{hour}", tmp_path / f"packet{hour}"
        database.write_bytes(seed)
        assert handoff.acquire("nikkei_indexes", SOURCE, DAY, packet, database) == 0
        handoff.validate_handoff(packet, "nikkei_indexes", SOURCE)
        assert control.calls == [("pbr", 10), ("pbr", 11)]
        raw = packet / "receipts.sqlite"
        report = apply(raw, canonical, tmp_path / f"accepted{hour}")
        assert bool(report["late_recovery_required"]) == (hour == 2)
        if hour == 0:
            seed = canonical.read_bytes()
    row, state = current(canonical)
    assert row.records == (
        {"date_text": "2026.11.06", "index_based_text": "2.80", "weighted_text": "2.01"},
    )
    assert row.fetched_at == STAMP
    assert state[0] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")
    control.hour, control.fail, control.calls = 4, True, []
    database, packet = tmp_path / "failed-input", tmp_path / "failed-packet"
    database.write_bytes(canonical.read_bytes())
    assert handoff.acquire("nikkei_indexes", "", DAY, packet, database) == 1
    raw = packet / "receipts.sqlite"
    with closing(initialize_database(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM economic_source_records WHERE source_id=?", (SOURCE,)
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM economic_source_records WHERE source_id='nikkei-per'"
        ).fetchone()
        assert (
            connection.execute(
                "SELECT status FROM economic_source_runs WHERE source_id=?", (SOURCE,)
            ).fetchone()[0]
            == "failed"
        )
    assert not apply(raw, canonical, tmp_path / "failed-accepted")["late_recovery_required"]
    assert current(canonical) == (row, state)
    assert current(canonical, source_id="nikkei-per")[1][0] == (
        STAMP + timedelta(hours=4)
    ).isoformat(timespec="microseconds")
