"""A new date must not materialize omitted historical children, even across billing months."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.engine.machine_analysis.generation.test_economic_fixed_inputs import (
    ASOF,
    CLOCK,
    EVALUATED,
    current,
    root,
    run,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation import economic_inputs as inputs
from baibai_engine.machine_analysis.generation import economic_membership as membership


def documents(path, keys):
    with closing(initialize_database(path)) as connection, connection:
        for key in keys:
            store_document(
                connection,
                SourceDocument(
                    source_id="matsui-margin",
                    request_key=key,
                    fetched_at=EVALUATED,
                    source_url="https://example.invalid/fixture",
                    content_type="text/html",
                    payload=b"<html>unreadable</html>",
                    metadata={"receipt_jst_date": "2023-01-01"},
                ),
            )


def seed_history(path):
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK,
            records_by_day={
                date(year, 1, 1) + timedelta(days=i): [{"PAYEMS": str(100_000 + i)}]
                for year in (2023, 2024, 2025)
                for i in range(160)
            },
        )
    run(path, "latest-failed")
    documents(path, [date(2020 + i // 12, i % 12 + 1, 1).isoformat() for i in range(40)])


def legacy(store, path, moment=EVALUATED):
    return inputs._retain_economic_input(
        lambda value: inputs._retain(store, value),
        path,
        as_of=ASOF,
        evaluation_time=moment,
        rules_path=inputs.DEFAULT_RULES_PATH,
    )


def retain(store, path, moment=EVALUATED):
    return inputs.retain_economic_input(store, path, as_of=ASOF, evaluation_time=moment)


def reset_counts(store):
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()


def usage(store):
    return (
        len(store.put_keys),
        len(store.get_keys) + len(store.head_keys),
        sum(len(store.values[key].body) for key in store.put_keys),
        sum(len(item.body) for item in store.values.values()),
    )


def test_hybrid_cold_rebuild_keeps_exact_sources_and_bounds_each_billing_period(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_history(path)
    old, new = MemoryR2Store(), MemoryR2Store()
    baseline, fixed = legacy(old, path), retain(new, path)
    assert baseline.descriptor["format"] == "economic-input-v2"
    assert fixed.descriptor["format"] == "economic-input-v3"

    def budget():
        assert all(a <= b for a, b in zip(usage(new), usage(old), strict=True))
        old_root = old.values[inputs._key(baseline.descriptor["selection_sha256"])].body
        new_root = new.values[inputs._key(fixed.descriptor["selection_sha256"])].body
        assert len(new_root) <= len(old_root)
        header, inline, _ = membership.unpack_root(json.loads(new_root))
        assert set(inline) == {"fred-payems/2023", "fred-payems/2024", "fred-payems/2025"}
        assert header["months"] == {
            key: digest
            for key, digest in json.loads(old_root)["months"].items()
            if key not in inline
        }
        assert len(new.values) == len(old.values) - len(inline)
        assert inputs.rebuild_economic_input(new, path, fixed).context == (
            inputs.rebuild_economic_input(old, path, baseline).context
        )

    budget()
    # The previous whole-root design materialized the other 40 children when one
    # incompressible block arrived. Check this day on its own, not an amortized sum.
    documents(path, ["new-" + hashlib.sha256(str(i).encode()).hexdigest() for i in range(550)])
    reset_counts(old)
    reset_counts(new)
    next_month = EVALUATED + timedelta(days=31)
    baseline, fixed = legacy(old, path, next_month), retain(new, path, next_month)
    assert len(new.put_keys) == len(old.put_keys) == 2  # new document block + root
    budget()
    reset_counts(new)
    assert retain(new, path, next_month) == fixed
    assert not new.put_keys

    with sqlite3.connect(path) as database:
        database.execute("UPDATE economic_source_records SET record_count=11")
    with pytest.raises(ValueError, match="original failed integrity"):
        inputs.rebuild_economic_input(new, path, fixed)
    with sqlite3.connect(path) as database:
        database.execute("DELETE FROM economic_source_documents WHERE request_key='2020-01-01'")
    with pytest.raises(ValueError, match="original is unavailable"):
        inputs.rebuild_economic_input(new, path, fixed)


def test_existing_legacy_selection_is_never_repacked_or_left_incomplete(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_history(path)
    store = MemoryR2Store()
    baseline = legacy(store, path)
    saved_keys = set(store.values)
    reset_counts(store)
    assert retain(store, path) == baseline
    assert not store.put_keys
    assert len(store.get_keys) == len(saved_keys)
    lost = inputs._key(root(store, baseline)["months"]["fred-payems/2024"])
    del store.values[lost]  # this child would normally be inline in a new selection
    assert retain(store, path) == baseline
    assert store.put_keys == [lost]
    assert set(store.values) == saved_keys
    assert inputs.rebuild_economic_input(store, path, baseline).context == current(path)


def test_calendar_preserves_weekday_exceptions_empty_days_and_two_exact_clocks():
    fetched = CLOCK.isoformat(timespec="microseconds")
    checked = EVALUATED.isoformat(timespec="microseconds")
    name = "test-source/2024"
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(366)]
    rows = [
        inputs._Receipt(
            key=("test-source", day.isoformat(), fetched),
            sha256="a" * 64,
            confirmed=day.weekday() != 1,
            checked_at=checked if day.weekday() == 1 else None,
        )
        for day in days
        if (day.weekday() < 5 and day.isoformat() != "2024-07-04")
        or day.isoformat() == "2024-12-29"
    ]
    block = {"records": rows, "documents": []}
    text = membership.inline_year(name, block)
    assert text is not None
    assert len(text) <= 64
    assert list(membership.year_members(name, text)) == [
        ("records", row.key, row.confirmed, row.checked_at, None) for row in rows
    ]
    for errors in (text[:5], "~not base64", text * 2):
        with pytest.raises(ValueError, match="invalid inline"):
            list(membership.year_members(name, errors))
    assert membership.inline_year(name, {"records": rows[:31], "documents": []}) is None
    assert membership.inline_year(name, {"records": [], "documents": rows}) is None
    rows[0] = rows[0].model_copy(update={"checked_at": None, "confirmed": False})
    assert membership.inline_year(name, block) is None  # third state uses its original child
