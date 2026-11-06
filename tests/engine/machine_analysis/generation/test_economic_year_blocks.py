"""Yearly record metadata preserves receipts and the old monthly wire layout."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta

from tests.engine.machine_analysis.generation.test_economic_fixed_inputs import (
    CLOCK,
    current,
    freeze,
    root,
    run,
)
from tests.engine.machine_analysis.generation.test_economic_fixed_transport import seed_months
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.economic_inputs import rebuild_economic_input
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES

PREFIX = "fixed-inputs/economic/v1/"


def block(store, digest):
    return json.loads(store.values[PREFIX + digest + ".json"].body)


def old_monthly_layout(store, saved):
    """Build the prior wire layout, without relaxing the saved recipe check."""

    def retain(value):
        payload = canonical_json(value).encode()
        digest = hashlib.sha256(payload).hexdigest()
        store.store_object(PREFIX + digest + ".json", payload)
        return digest

    selection = root(store, saved)
    groups = defaultdict(lambda: {"records": [], "documents": []})
    for digest in selection["months"].values():
        for kind, receipts in block(store, digest).items():
            for receipt in receipts:
                source, day, _ = receipt["key"]
                month = day[:7] if len(day) >= 10 and day[4:5] == "-" else "documents"
                groups[source + "/" + month][kind].append(receipt)
    selection["months"] = {key: retain(value) for key, value in sorted(groups.items())}
    descriptor = {"format": "economic-input-v2", "selection_sha256": retain(selection)}
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1", reference="economic-" + digest, sha256=digest, descriptor=descriptor
    )


def test_year_blocks_keep_monthly_documents_and_both_kinds_of_empty_receipt(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    with closing(initialize_database(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id="fred-payems",
            observed_at=date(2023, 1, 2),
            fetched_at=CLOCK,
            records=[],
        )
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK + timedelta(minutes=1),
            records_by_day={
                date(year, month, 1): [{"PAYEMS": str(100000 + month)}]
                for year in (2023, 2024, 2025)
                for month in range(1, 13)
                if (year, month) != (2024, 4)
            },
        )
        for month in (11, 12):
            day = f"2025-{month}-05"
            store_document(
                connection,
                SourceDocument(
                    source_id="matsui-margin",
                    request_key=day,
                    fetched_at=CLOCK,
                    source_url="https://example.invalid/fixture",
                    content_type="text/html",
                    payload=b"unreadable original",
                    metadata={"receipt_jst_date": day},
                ),
            )
    original = path.read_bytes()
    store = MemoryR2Store()
    saved = freeze(store, path)
    selection = root(store, saved)
    assert set(selection["months"]) == {
        "fred-payems/2023",
        "fred-payems/2024",
        "fred-payems/2025",
        "matsui-margin/2025-11",
        "matsui-margin/2025-12",
    }
    receipts = {
        receipt["key"][1]: receipt
        for digest in selection["months"].values()
        for receipt in block(store, digest)["records"]
    }
    assert len(receipts) == 37
    assert receipts["2023-01-02"]["confirmed"] is False
    assert receipts["2024-04-01"]["confirmed"] is True
    assert receipts["2024-04-01"]["key"][2] == (CLOCK + timedelta(minutes=1)).isoformat(
        timespec="microseconds"
    )
    assert rebuild_economic_input(store, path, saved).context == current(path)
    assert path.read_bytes() == original


def test_old_monthly_root_rebuilds_without_rewriting_it_after_later_corrections(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    run(path, "original-failure")
    store = MemoryR2Store()
    yearly = freeze(store, path)
    monthly = old_monthly_layout(store, yearly)
    assert len(root(store, monthly)["months"]) == 36
    expected = current(path)
    assert rebuild_economic_input(store, path, monthly).context == expected
    retained = {key: value.body for key, value in store.values.items()}
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK + timedelta(minutes=2),
            records_by_day={date(2026, 1, 1): [{"PAYEMS": "200000"}]},
        )
    run(path, "later-success", status="ok", at=CLOCK + timedelta(minutes=3))
    after = path.read_bytes()
    assert current(path) != expected
    for saved in (monthly, yearly):
        assert rebuild_economic_input(store, path, saved).context == expected
    assert rebuild_economic_input(store, path, freeze(store, path)).context == current(path)
    assert all(store.values[key].body == body for key, body in retained.items())
    assert path.read_bytes() == after


def test_unchanged_years_are_reused_and_warm_transport_scales_with_year_blocks(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    store = MemoryR2Store()
    first = freeze(store, path)
    expected = current(path)
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()
    assert freeze(store, path) == first
    assert rebuild_economic_input(store, path, first).context == expected
    # Three yearly record blocks and one root, read once for retain and rebuild.
    assert len(store.head_keys) == 8
    assert len(store.get_keys) == 8
    assert not store.put_keys
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK + timedelta(minutes=1),
            records_by_day={
                date(year, month, 1): [
                    {"PAYEMS": "200000" if (year, month) == (2025, 4) else str(100000 + month)}
                ]
                for year in (2023, 2024, 2025)
                for month in range(1, 13)
            },
        )
    second = freeze(store, path)
    old, new = root(store, first)["months"], root(store, second)["months"]
    assert {key for key in old if old[key] != new[key]} == {"fred-payems/2025"}
    assert len(store.put_keys) == 2  # Changed year plus new acquisition-state root.
    assert rebuild_economic_input(store, path, first).context == expected
    assert rebuild_economic_input(store, path, second).context == current(path)


def test_dense_leap_year_keeps_every_receipt_below_existing_object_limit(tmp_path):
    path = tmp_path / "economic.sqlite"
    days = {date(2024, 1, 1) + timedelta(days=i) for i in range(366)}
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK,
            records_by_day={day: [{"PAYEMS": "100000"}] for day in days},
        )
    store = MemoryR2Store()
    saved = freeze(store, path)
    assert set(root(store, saved)["months"]) == {"fred-payems/2024"}
    digest = root(store, saved)["months"]["fred-payems/2024"]
    assert len(store.values[PREFIX + digest + ".json"].body) < MAX_PRIVATE_OBJECT_BYTES
    assert {receipt["key"][1] for receipt in block(store, digest)["records"]} == {
        day.isoformat() for day in days
    }
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 366
        )
    assert rebuild_economic_input(store, path, saved).context == current(path)
