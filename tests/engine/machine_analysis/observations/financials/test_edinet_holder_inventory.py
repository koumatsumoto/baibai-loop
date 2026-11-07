"""通常の固定pair在庫と有限legacy SQL在庫は、本文readerだけで混同しない。"""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_holder_native import (
    DAY,
    KNOWN,
    native_for,
    read,
    seed,
)

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
    read_fixed_holder_capital,
)
from baibai_engine.storage.objects.r2 import LakePublishError


def fixed_for(items, *, stored_on="2026-11-02", received_at=None, source_type=1):
    store, archives = native_for(items)
    value = archives.value()
    for entry in value["archives"]:
        entry.update(stored_on=stored_on, received_at_utc=received_at, source_type=source_type)
    return store, FixedEdinetArchives(store, value)


def read_fixed(connection, archives, *, selected_at=None, known_at=KNOWN, as_of=DAY, seal=None):
    return read_fixed_holder_capital(
        connection,
        as_of=as_of,
        known_at=known_at,
        tickers=("1234",),
        snapshot_sealed_at=seal,
        archives=archives,
        selected_at=selected_at or min(known_at, seal or known_at),
    )


@pytest.mark.parametrize("sql_day", [None, "2026-11-01", "2026-11-04", "not-a-day"])
def test_normal_native_inventory_is_independent_of_missing_or_stale_sql(sql_day):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, retained=sql_day is not None, stored_on=sql_day)
        store, archives = fixed_for({"S100TEST": body})
        queries = []
        connection.set_trace_callback(queries.append)

        def deny_raw(action, table, column, *unused):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "edinet_source_archives"
                else sqlite3.SQLITE_OK
            )

        connection.set_authorizer(deny_raw)
        observations, coverage = read_fixed(connection, archives)
        assert coverage == "all_retained_considered"
        fact = observations["1234"][0]
        assert fact.document_id == "S100TEST"
        assert fact.total_stocks == 2500000
        assert fact.original_sha256 == hashlib.sha256(body).hexdigest()
        assert not any("edinet_source_archives" in query for query in queries)
        assert len(store.get_keys) == 1
        assert store.put_keys == []
        assert not any(key.endswith("head.json") for key in store.head_keys + store.get_keys)


def test_sql_membership_cannot_expand_fixed_native_inventory():
    with closing(sqlite3.connect(":memory:")) as connection:
        seed(connection)
        _store, archives = fixed_for({})
        assert read(connection)[0]
        assert read_fixed(connection, archives) == ({}, "partial")


@pytest.mark.parametrize("source_type", [1, 5])
def test_global_coverage_includes_other_issuers_and_excludes_csv(source_type):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, retained=False)
        seed(connection, issuer="7777", doc_id="S100MISS", seq=2, retained=False)
        store, archives = fixed_for({"S100TEST": body}, source_type=source_type)
        observations, coverage = read_fixed(connection, archives)
        assert bool(observations) is (source_type == 1)
        assert coverage == "partial"
        assert len(store.get_keys) == int(source_type == 1)


@pytest.mark.parametrize("damage", ["missing", "changed", "invalid_zip"])
def test_normal_damaged_native_does_not_fall_back_to_good_sql(damage):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        store, archives = fixed_for({"S100TEST": b"not a ZIP" if damage == "invalid_zip" else body})
        key = next(iter(store.values))
        if damage == "missing":
            del store.values[key]
        elif damage == "changed":
            store.store_object(key, b"changed", sha256=hashlib.sha256(body).hexdigest())
        if damage == "invalid_zip":
            assert read_fixed(connection, archives) == ({}, "partial")
        else:
            with pytest.raises(LakePublishError):
                read_fixed(connection, archives)
        assert read(connection)[0]


@pytest.mark.parametrize("precise", [False, True])
@pytest.mark.parametrize(
    ("stored_on", "known_at", "as_of", "seal", "accepted"),
    [
        ("2026-11-02", KNOWN, DAY, None, True),
        ("2026-11-03", KNOWN, DAY, None, False),
        ("2026-11-03", KNOWN, DAY, KNOWN - timedelta(hours=5), True),
        ("2026-11-03", KNOWN, DAY, KNOWN, True),
        ("2026-11-03", KNOWN, DAY, KNOWN + timedelta(seconds=1), False),
        ("2026-11-02", datetime(2026, 11, 2, 16, tzinfo=UTC), DAY, None, False),
        (
            "2026-11-02",
            datetime(2026, 11, 2, 16, tzinfo=UTC),
            DAY,
            datetime(2026, 11, 2, 15, 30, tzinfo=UTC),
            True,
        ),
        (
            "2026-11-03",
            datetime(2026, 11, 4, 1, tzinfo=UTC),
            DAY,
            datetime(2026, 11, 3, 16, tzinfo=UTC),
            False,
        ),
        (
            "2026-11-04",
            datetime(2026, 11, 4, 8, tzinfo=UTC),
            date(2026, 11, 4),
            datetime(2026, 11, 3, 7, tzinfo=UTC),
            False,
        ),
    ],
)
def test_normal_keeps_coarse_receipt_day_and_seal_boundaries(
    precise, stored_on, known_at, as_of, seal, accepted
):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, stored_on=stored_on)
        received_at = stored_on + "T00:00:00+00:00" if precise else None
        store, archives = fixed_for(
            {"S100TEST": body}, stored_on=stored_on, received_at=received_at
        )
        expected = read(connection, known_at=known_at, as_of=as_of, seal=seal)
        assert (
            read_fixed(connection, archives, known_at=known_at, as_of=as_of, seal=seal) == expected
        )
        assert bool(expected[0]) is accepted
        assert len(store.get_keys) == int(accepted)


@pytest.mark.parametrize(
    "cutoff", ["selection_naive", "knowledge_naive", "seal_naive", "knowledge", "seal"]
)
def test_normal_selection_must_be_known_by_knowledge_and_seal(cutoff):
    _store, archives = fixed_for({})
    values = dict(selected_at=KNOWN, known_at=KNOWN, seal=KNOWN)
    if cutoff.endswith("_naive"):
        key = {
            "selection_naive": "selected_at",
            "knowledge_naive": "known_at",
            "seal_naive": "seal",
        }[cutoff]
        values[key] = KNOWN.replace(tzinfo=None)
    else:
        values["known_at" if cutoff == "knowledge" else "seal"] -= timedelta(microseconds=1)
    with (
        closing(sqlite3.connect(":memory:")) as connection,
        pytest.raises(ValueError, match=r"timezone|selected after"),
    ):
        read_fixed(connection, archives, **values)


@pytest.mark.parametrize("stored_on", ["2026-11-02", "2026-11-04"])
def test_normal_uses_native_day_correction_for_edits_without_sql_equality_gate(stored_on):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, stored_on="2026-11-04", edit_day="2026-11-03")
        store, archives = fixed_for({"S100TEST": body}, stored_on=stored_on)
        arguments = dict(as_of=date(2026, 11, 5), known_at=datetime(2026, 11, 5, 8, tzinfo=UTC))
        assert read(connection, **arguments)[0]
        result = read_fixed(connection, archives, **arguments)
        assert bool(result[0]) is (stored_on == "2026-11-04")
        assert result[1] == ("all_retained_considered" if result[0] else "partial")
        assert len(store.get_keys) == int(bool(result[0]))


@pytest.mark.parametrize(
    "selected",
    [
        None,
        "invalid",
        "2026-10-30T06:00:00",
        "2026-11-03T12:00:01+00:00",
        "2026-11-03T15:00:00+00:00",
    ],
)
def test_normal_unknown_or_late_selected_list_receipt_is_not_success(selected):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, retained=False)
        connection.execute(
            "UPDATE edinet_document_lists SET selected_received_at_utc=?", (selected,)
        )
        store, archives = fixed_for({"S100TEST": body})
        assert read_fixed(connection, archives) == ({}, "partial")
        assert store.get_keys == []


def test_normal_uses_selected_not_latest_list_and_keeps_withdrawals():
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, retained=False)
        connection.execute(
            "UPDATE edinet_document_lists SET latest_received_at_utc='2026-11-04T20:00:00+00:00'"
        )
        store, archives = fixed_for({"S100TEST": body})
        assert read_fixed(connection, archives)[0]["1234"]
        connection.execute("UPDATE edinet_documents SET withdrawal_status='2',legal_status='0'")
        store.get_keys.clear()
        assert read_fixed(connection, archives) == ({}, "not_observed")
        assert store.get_keys == []
