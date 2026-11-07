"""FRED acquisition needs a complete response, not a second native history."""

import gzip
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.fred as fred
import baibai_engine.data_collection.receipts.native_sources.store as native_store
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_jobs.acquisition.economic_staging import load_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = SourceDefinition(
    "fred-test", "fred_csv", "https://example.test/fred.csv", "monthly", "TEST"
)
GROUP = "economic-fred_csv"
WHEN = datetime(2026, 11, 1, tzinfo=UTC)
DAY = date(2026, 10, 1)
OTHER = date(2026, 9, 1)
TABLES = (*native_store.RECEIPT_KEYS, *native_store.CONFIRMATION_TABLES)


def csv(values):
    return "observation_date,TEST\n" + "".join(
        f"{day},{value}\n" for day, value in sorted(values.items())
    )


def rows(path, table):
    with closing(native_store.open_read_only_connection(path)) as conn:
        return sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))


def state(path):
    return {table: rows(path, table) for table in TABLES}


def receive(path, hour, values):
    with closing(native_store.initialize_database(path)) as conn, conn:
        confirm_source_response(
            conn,
            source_id=SOURCE.source_id,
            fetched_at=WHEN + timedelta(hours=hour),
            records_by_day=fred.parse_fred_csv(SOURCE, csv(values)),
        )


@pytest.fixture
def work(tmp_path):
    return tmp_path


@pytest.fixture
def harness(monkeypatch):
    control = SimpleNamespace(clock=WHEN, payload="", requests=[])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def fetch(_session, url, **kwargs):
        control.requests.append((url, kwargs.get("params")))
        if isinstance(control.payload, Exception):
            raise control.payload
        return control.payload

    for module in (acquisition, native_store, handoff, inputs):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "load_sources", lambda: {SOURCE.source_id: SOURCE})
    monkeypatch.setattr(inputs, "load_economic_sources", lambda: {SOURCE.source_id: SOURCE})
    monkeypatch.setattr(handoff, "load_economic_sources", lambda: {SOURCE.source_id: SOURCE})
    monkeypatch.setattr(fred, "fetch_text", fetch)
    monkeypatch.setattr(
        native_store.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=f"run{int(control.clock.timestamp())}"),
    )

    def acquire(root, seed, mode, hour, payload):
        root.mkdir()
        control.clock = WHEN + timedelta(hours=hour)
        control.payload = payload
        remote = MemoryR2Store()
        with monkeypatch.context() as scoped:
            if mode == "full":
                # Old generation-bound full inputs remain readable after the writer switches.
                scoped.setattr(
                    inputs,
                    "export_economic_receipts",
                    lambda source, target, **kw: export_receipts(
                        source, target, source_ids=[SOURCE.source_id], since=kw["since"]
                    ),
                )
            published = inputs.publish_inputs(
                remote,
                market=root / "unused",
                economic=seed,
                identity="test",
                asof=WHEN.date(),
                generations={"economic_etag": "fixed-seed"},
                only_group=GROUP,
            )
        assert published == {"updated": [GROUP], "failed": []}
        database = root / "input.sqlite"
        metadata = inputs.load_input(remote, GROUP, database, root / "loaded")
        assert metadata["canonical_generations"] == {"economic_etag": "fixed-seed"}
        seed_rows = state(database)
        if mode == "empty":
            assert all(not values for values in seed_rows.values())
        request_count = len(control.requests)
        packet = root / "packet"
        status = handoff.acquire("fred_csv", "", WHEN.date(), packet, database)
        assert len(control.requests) == request_count + 1
        assert control.requests[-1] == (SOURCE.source_url, None)
        handoff.validate_handoff(packet, "fred_csv")
        report = json.loads((packet / "result.json").read_text())
        assert report["sources"][0]["history_recovery_required"] is False
        return packet / "receipts.sqlite", status, report, seed_rows

    return acquire


def integrate(raw, canonical, accepted):
    outcome = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return outcome


@pytest.mark.parametrize(
    "values",
    [
        {DAY: "A", OTHER: "B"},
        {DAY: "C", OTHER: "B"},
        {DAY: "A"},
        {DAY: ".", OTHER: "B"},
        {DAY: "A", OTHER: "B", date(1905, 1, 1): "new-old-period"},
    ],
)
def test_latest_complete_response_canonical_exact_match(work, harness, values):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A", OTHER: "B"})
    original = seed.read_bytes()
    final = []
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original)
        raw, status, report, _ = harness(work / mode, seed, mode, 2, csv(values))
        assert status == 0
        assert report["sources"][0]["status"] == "ok"
        outcome = integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        assert not outcome["late_recovery_required"]
        final.append(state(canonical))
    assert final[0] == final[1]
    assert seed.read_bytes() == original
    assert set(rows(seed, "economic_source_records")) <= set(final[0]["economic_source_records"])


def test_stale_seed_a_b_a_and_canonical_only_disappearance(work, harness):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A"})
    final = []
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed.read_bytes())
        # Neither old full seed nor empty seed knows this canonical-only period.
        receive(canonical, 1, {DAY: "A", OTHER: "unknown-to-seed"})
        for index, value in enumerate(("B", "A"), 2):
            raw, status, _, _ = harness(
                work / f"{mode}-{index}", seed, mode, index, csv({DAY: value})
            )
            assert status == 0
            outcome = integrate(raw, canonical, work / f"accepted-{mode}-{index}.sqlite")
            assert not outcome["late_recovery_required"]
        final.append(state(canonical))
    assert final[0] == final[1]
    records = final[0]["economic_source_records"]
    assert [(row[2], row[3]) for row in records if row[1] == str(OTHER)][-1] == (
        (WHEN + timedelta(hours=2)).isoformat(timespec="microseconds"),
        0,
    )
    assert [
        json.loads(gzip.decompress(row[4]))[0]["TEST"] for row in records if row[1] == str(DAY)
    ] == ["A", "B", "A"]


@pytest.mark.parametrize(
    "payload",
    [
        requests.RequestException("synthetic timeout"),
        "observation_date,TEST\n",
        "observation_date,TEST\n2026-10-01\n",
        "observation_date,TEST,TEST\n2026-10-01,1,2\n",
    ],
)
def test_failure_keeps_canonical_originals_and_confirmation(work, harness, payload):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A", OTHER: "B"})
    before = state(seed)
    final = []
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed.read_bytes())
        raw, status, report, _ = harness(work / mode, seed, mode, 2, payload)
        assert status == 1
        assert report["sources"][0]["status"] == "failed"
        outcome = integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        assert not outcome["late_recovery_required"]
        after = state(canonical)
        for table in TABLES:
            if table != "economic_source_runs":
                assert after[table] == before[table]
        assert after["economic_source_runs"][0][6] == "failed"
        final.append(after)
    assert final[0] == final[1]


CASES = [
    ({DAY: "A"}, [(3, {DAY: "A"})], {DAY: "A"}, False),
    ({DAY: "A"}, [(3, {DAY: "B"})], {DAY: "A"}, False),
    ({DAY: "A"}, [(3, {DAY: "A", OTHER: "new-at-3"})], {DAY: "A"}, False),
    ({DAY: "A"}, [(1, {DAY: "B"}), (3, {DAY: "A"})], {DAY: "A"}, True),
    ({DAY: "A"}, [(3, {DAY: "A"})], {DAY: "A", OTHER: "unseen-at-2"}, True),
    ({DAY: "A", OTHER: "B"}, [(3, {DAY: "A", OTHER: "B"})], {DAY: "A"}, True),
    ({DAY: "A"}, [(1, {DAY: "A", OTHER: "B"}), (3, {DAY: "A"})], {DAY: "A"}, True),
    ({DAY: "A", OTHER: "B"}, [(1, {DAY: "A"}), (3, {DAY: "A"})], {DAY: "A"}, False),
    ({DAY: "A"}, [(2, {DAY: "B"}), (3, {DAY: "A"})], {DAY: "B"}, False),
]


@pytest.mark.parametrize(
    ("initial", "history", "response", "late"),
    CASES,
    ids=[
        "same-since-0",
        "changed-only-after-response",
        "period-first-seen-later",
        "earlier-a-b-a-return",
        "unreceived-period",
        "omitted-period-then-restored",
        "earlier-disappearance",
        "already-proven-absence",
        "exact-clock-replay",
    ],
)
def test_complete_late_response_and_actual_receipt_clock(
    work, harness, initial, history, response, late
):
    seed = work / "seed.sqlite"
    receive(seed, 0, initial)
    seed_bytes = seed.read_bytes()
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed_bytes)
        for hour, values in history:
            receive(canonical, hour, values)
        before = state(canonical)
        raw, status, _, _ = harness(work / mode, seed, mode, 2, csv(response))
        assert status == 0
        raw_bytes = raw.read_bytes()
        checked = rows(raw, "economic_source_collection_confirmations")[0][1]
        assert checked == (WHEN + timedelta(hours=2)).isoformat(timespec="microseconds")
        outcome = integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        assert bool(outcome["late_recovery_required"]) is late, (mode, outcome)
        assert raw.read_bytes() == raw_bytes
        assert seed.read_bytes() == seed_bytes
        after = state(canonical)
        assert all(
            after[table] == before[table] for table in TABLES if table != "economic_source_runs"
        )
        assert after["economic_source_runs"][-1][4] == checked


@pytest.mark.parametrize("at_two", [{DAY: "B"}, {DAY: "A", OTHER: "B"}])
def test_old_complete_confirmation_same_clock_conflict_is_rejected(work, harness, at_two):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A"})
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed.read_bytes())
        receive(canonical, 2, at_two)
        receive(canonical, 3, {DAY: "A"})
        raw, status, _, _ = harness(work / mode, seed, mode, 2, csv({DAY: "A"}))
        assert status == 0
        before, raw_bytes = canonical.read_bytes(), raw.read_bytes()
        # A full input carries A@0 plus the real @2 recheck, rather than a new
        # A@2 raw row. The complete response still conflicts with known B@2.
        with pytest.raises(EconomicReceiptConflict):
            integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        assert canonical.read_bytes() == before
        assert raw.read_bytes() == raw_bytes


def test_legacy_unconfirmed_empty_is_not_historical_absence_proof(work, harness):
    seed = work / "seed.sqlite"
    with closing(native_store.initialize_database(seed)) as conn, conn:
        for day, value in ((DAY, "A"), (OTHER, "B")):
            store_snapshot(
                conn,
                source_id=SOURCE.source_id,
                observed_at=day,
                fetched_at=WHEN,
                records=[{"observation_date": str(day), "TEST": value}],
            )
        store_snapshot(
            conn,
            source_id=SOURCE.source_id,
            observed_at=OTHER,
            fetched_at=WHEN + timedelta(hours=1),
            records=[],
        )
    assert not rows(seed, "economic_source_record_confirmations")
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed.read_bytes())
        receive(canonical, 3, {DAY: "A"})
        # The real current absence anchor is @3, not the unconfirmed [] at @1.
        absent = next(
            row
            for row in rows(canonical, "economic_source_record_confirmations")
            if row[1] == str(OTHER)
        )
        assert absent[3] == (WHEN + timedelta(hours=3)).isoformat(timespec="microseconds")
        before = state(canonical)
        raw, status, _, _ = harness(work / mode, seed, mode, 2, csv({DAY: "A"}))
        assert status == 0
        outcome = integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        assert outcome["late_recovery_required"]
        after = state(canonical)
        assert all(
            after[table] == before[table] for table in TABLES if table != "economic_source_runs"
        )


def test_same_payload_with_different_record_count_is_not_noop(work, harness):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A"})
    canonical = work / "canonical.sqlite"
    canonical.write_bytes(seed.read_bytes())
    receive(canonical, 3, {DAY: "A"})
    raw, status, _, _ = harness(work / "empty", seed, "empty", 2, csv({DAY: "A"}))
    assert status == 0
    with closing(native_store.open_connection(raw)) as conn, conn:
        conn.execute("UPDATE economic_source_records SET record_count=2")
    before = state(canonical)
    outcome = integrate(raw, canonical, work / "accepted.sqlite")
    assert outcome["late_recovery_required"]
    after = state(canonical)
    assert all(after[table] == before[table] for table in TABLES if table != "economic_source_runs")


def test_actual_new_input_is_empty_and_fixed_old_input_remains_readable(work, harness, monkeypatch):
    seed = work / "canonical.sqlite"
    receive(seed, 0, {DAY: "A", OTHER: "B"})
    original = seed.read_bytes()
    remote = MemoryR2Store()
    with monkeypatch.context() as patch:
        patch.setattr(
            inputs,
            "export_economic_receipts",
            lambda source, target, **kw: export_receipts(
                source, target, source_ids=[SOURCE.source_id], since=kw["since"]
            ),
        )
        assert inputs.publish_inputs(
            remote,
            market=work / "unused",
            economic=seed,
            identity="legacy",
            asof=WHEN.date(),
            generations={"economic_etag": "old"},
            only_group=GROUP,
        ) == {"updated": [GROUP], "failed": []}
    old = work / "old.sqlite"
    inputs.load_input(remote, GROUP, old, work / "old")
    assert rows(old, "economic_source_records")
    # This invocation exercises the candidate's actual branch, without replacing
    # its export function. No new input format or implicit missing seed is used.
    assert inputs.publish_inputs(
        remote,
        market=work / "unused",
        economic=seed,
        identity="new",
        asof=WHEN.date(),
        generations={"economic_etag": "new"},
        only_group=GROUP,
    ) == {"updated": [GROUP], "failed": []}
    new = work / "new.sqlite"
    metadata = inputs.load_input(remote, GROUP, new, work / "new")
    assert metadata["canonical_generations"] == {"economic_etag": "new"}
    assert all(not values for values in state(new).values())
    load_handoff(
        remote, GROUP + "-legacy", work / "fixed-old", namespace="source-inputs", input_group=GROUP
    )
    assert (work / "fixed-old/receipts.sqlite").read_bytes() == old.read_bytes()
    assert seed.read_bytes() == original


def test_unseen_intermediate_change_is_not_hidden_by_equal_final_response(work, harness):
    seed, canonical = work / "seed.sqlite", work / "canonical.sqlite"
    receive(seed, 0, {DAY: "A"})
    canonical.write_bytes(seed.read_bytes())
    receive(canonical, 3, {DAY: "A"})
    raw, status, _, _ = harness(work / "empty", seed, "empty", 2, csv({DAY: "A"}))
    assert status == 0
    with closing(native_store.open_connection(raw)) as conn, conn:
        store_snapshot(
            conn,
            source_id=SOURCE.source_id,
            observed_at=DAY,
            fetched_at=WHEN + timedelta(hours=1),
            records=[{"observation_date": str(DAY), "TEST": "B"}],
        )
    raw_bytes, before = raw.read_bytes(), state(canonical)
    outcome = integrate(raw, canonical, work / "accepted.sqlite")
    assert outcome["late_recovery_required"]
    assert raw.read_bytes() == raw_bytes
    after = state(canonical)
    assert all(after[table] == before[table] for table in TABLES if table != "economic_source_runs")


def test_calendar_only_empty_does_not_create_a_historical_period(work, harness):
    seed, canonical = work / "seed.sqlite", work / "canonical.sqlite"
    receive(seed, 0, {DAY: "A"})
    with closing(native_store.open_connection(seed)) as conn, conn:
        store_snapshot(
            conn,
            source_id=SOURCE.source_id,
            observed_at=OTHER,
            fetched_at=WHEN,
            records=[],
        )
    canonical.write_bytes(seed.read_bytes())
    receive(canonical, 3, {DAY: "A"})
    raw, status, _, _ = harness(work / "empty", seed, "empty", 2, csv({DAY: "A"}))
    assert status == 0
    before = state(canonical)
    outcome = integrate(raw, canonical, work / "accepted.sqlite")
    assert not outcome["late_recovery_required"]
    after = state(canonical)
    assert all(after[table] == before[table] for table in TABLES if table != "economic_source_runs")


def test_late_absence_without_retained_proof_stays_recoverable(work, harness):
    seed = work / "seed.sqlite"
    receive(seed, 0, {DAY: "A", OTHER: "B"})
    receive(seed, 1, {DAY: "A"})
    for mode in ("full", "empty"):
        canonical = work / f"canonical-{mode}.sqlite"
        canonical.write_bytes(seed.read_bytes())
        receive(canonical, 3, {DAY: "A", OTHER: "C"})
        before = state(canonical)
        raw, status, _, _ = harness(work / mode, seed, mode, 2, csv({DAY: "A"}))
        assert status == 0
        raw_bytes = raw.read_bytes()
        outcome = integrate(raw, canonical, work / f"accepted-{mode}.sqlite")
        # Current confirmation now points to C. Only the old full seed retains
        # proof that [] at @1 was a received absence, not a legacy calendar row.
        assert bool(outcome["late_recovery_required"]) is (mode == "empty")
        assert raw.read_bytes() == raw_bytes
        after = state(canonical)
        assert all(
            after[table] == before[table] for table in TABLES if table != "economic_source_runs"
        )
