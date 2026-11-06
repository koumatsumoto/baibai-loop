"""Fixed metadata references originals, including adverse and absent evidence."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview_market import CLOCK, capture

from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.economic_inputs import (
    rebuild_economic_input,
    retain_economic_input,
)
from baibai_engine.machine_analysis.generation.runners.market_reading import reading_snapshot
from baibai_engine.machine_analysis.generation.runners.reading_rules import DEFAULT_RULES_PATH
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    stored_market_context,
)
from baibai_engine.storage.objects.r2 import LakePublishError

ASOF = date(2026, 1, 5)
EVALUATED = CLOCK + timedelta(hours=1)
PERIOD = date(2026, 1, 1)


def seed(path, *, value="100000", at=CLOCK):
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=at,
            records_by_day={PERIOD: [{"PAYEMS": value}]},
        )


def run(path, identity, *, status="failed", at=CLOCK):
    with closing(initialize_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO economic_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identity,
                "fred-payems",
                "2026-01-01",
                "2026-01-05",
                at.isoformat(),
                at.isoformat(),
                status,
                1,
                "private diagnostic must not be saved",
            ),
        )


def freeze(store, path, **kwargs):
    return retain_economic_input(store, path, as_of=ASOF, evaluation_time=EVALUATED, **kwargs)


def current(path):
    return {
        "reading": reading_snapshot(path, asof=ASOF),
        "tradingview_market": stored_market_context(path, asof=ASOF, known_at=EVALUATED),
    }


def root(store, saved):
    return json.loads(
        store.values[
            "fixed-inputs/economic/v1/" + saved.descriptor["selection_sha256"] + ".json"
        ].body
    )


def test_cold_rebuild_uses_originals_not_current_adoption_runs_or_l2(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    run(path, "failed")
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture())
    expected = current(path)
    saved = freeze(store, path)
    assert rebuild_economic_input(store, path, saved) == expected
    original_bytes = path.read_bytes()
    seed(path, value="200000", at=CLOCK + timedelta(minutes=2))
    run(path, "recovered", status="ok", at=CLOCK + timedelta(minutes=3))
    with closing(initialize_database(path)) as c:
        retain_market_capture(
            c, capture(receipt_id="failed-later", error=True, received=CLOCK + timedelta(minutes=4))
        )
    assert path.read_bytes() != original_bytes
    assert current(path) != expected
    after = path.read_bytes()
    assert rebuild_economic_input(store, path, saved) == expected
    assert path.read_bytes() == after
    assert rebuild_economic_input(store, path, freeze(store, path)) == current(path)
    assert all(b"private diagnostic" not in item.body for item in store.values.values())
    assert all(b'PAYEMS": "100' not in item.body for item in store.values.values())
    assert all(item.content_type == "application/json" for item in store.values.values())


def test_same_response_changes_only_root_not_period_metadata_or_body(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    first = freeze(store, path)
    count = len(store.put_keys)
    seed(path, at=CLOCK + timedelta(minutes=3))
    second = freeze(store, path)
    assert first != second
    assert root(store, first)["months"] == root(store, second)["months"]
    assert len(store.put_keys) == count + 1
    with closing(sqlite3.connect(path)) as c, c:
        assert c.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
    assert rebuild_economic_input(store, path, first) == rebuild_economic_input(store, path, second)
    count = len(store.put_keys)
    assert freeze(store, path) == second
    assert len(store.put_keys) == count


def test_absent_run_and_empty_source_do_not_acquire_later_evidence(tmp_path):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)):
        pass
    store = MemoryR2Store()
    saved = freeze(store, path)
    expected = current(path)
    assert not root(store, saved)["months"]
    assert all(item is None for item in root(store, saved)["runs"].values())
    seed(path)
    run(path, "new-failure")
    assert rebuild_economic_input(store, path, saved) == expected
    assert current(path) != expected


@pytest.mark.parametrize("damage", ["missing", "payload", "record_count"])
def test_lost_or_changed_original_does_not_fall_back(tmp_path, damage):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    saved = freeze(store, path)
    seed(path, value="200000", at=CLOCK + timedelta(minutes=2))
    with closing(sqlite3.connect(path)) as c, c:
        if damage == "missing":
            c.execute(
                "DELETE FROM economic_source_records WHERE fetched_at=?",
                (CLOCK.isoformat(timespec="microseconds"),),
            )
        elif damage == "payload":
            c.execute(
                "UPDATE economic_source_records SET payload=? WHERE fetched_at=?",
                (b"changed", CLOCK.isoformat(timespec="microseconds")),
            )
        else:
            c.execute(
                "UPDATE economic_source_records SET record_count=99 WHERE fetched_at=?",
                (CLOCK.isoformat(timespec="microseconds"),),
            )
    with pytest.raises(ValueError, match="fixed economic original"):
        rebuild_economic_input(store, path, saved)


def test_current_confirmation_can_be_absent_without_reselecting(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    saved = freeze(store, path)
    expected = current(path)
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("DELETE FROM economic_source_record_confirmations")
        c.execute("DELETE FROM economic_source_collection_confirmations")
    assert rebuild_economic_input(store, path, saved) == expected


def test_disappeared_period_stays_empty_after_later_return(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    other = date(2026, 2, 1)
    with closing(initialize_database(path)) as c, c:
        confirm_source_response(
            c,
            source_id="fred-payems",
            fetched_at=CLOCK + timedelta(minutes=1),
            records_by_day={other: [{"PAYEMS": "100000"}]},
        )
    saved = freeze(store, path)
    expected = current(path)
    with closing(initialize_database(path)) as c, c:
        confirm_source_response(
            c,
            source_id="fred-payems",
            fetched_at=CLOCK + timedelta(minutes=2),
            records_by_day={PERIOD: [{"PAYEMS": "100000"}], other: [{"PAYEMS": "100000"}]},
        )
    assert current(path) != expected
    assert rebuild_economic_input(store, path, saved) == expected


def test_original_timestamp_spelling_is_not_reformatted_in_a_locator(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    seed(path)
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("UPDATE economic_source_records SET fetched_at=?", (CLOCK.isoformat(),))
        c.execute(
            "UPDATE economic_source_record_confirmations SET checked_at=?, adopted_fetched_at=?",
            (CLOCK.isoformat(), CLOCK.isoformat()),
        )
        c.execute(
            "UPDATE economic_source_collection_confirmations SET checked_at=?", (CLOCK.isoformat(),)
        )
    saved = freeze(store, path)
    assert rebuild_economic_input(store, path, saved) == current(path)


def test_lost_metadata_put_acknowledgement_reuses_exact_object(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed(path)

    class LostAckStore(MemoryR2Store):
        def put_file(self, *args, **kwargs):
            super().put_file(*args, **kwargs)
            raise LakePublishError("lost acknowledgement")

    store = LostAckStore()
    saved = freeze(store, path)
    assert len(store.put_keys) == len(store.values)
    assert rebuild_economic_input(store, path, saved) == current(path)


@pytest.mark.parametrize("count", [200, 201, 205])
def test_tradingview_boundary_keeps_unusable_receipts_and_201st(tmp_path, count):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    with closing(initialize_database(path)) as c:
        for i in range(count):
            retain_market_capture(
                c,
                capture(
                    receipt_id=f"receipt-{i:03d}", received=CLOCK + timedelta(seconds=i), error=True
                ),
            )
    expected = current(path)
    assert expected["tradingview_market"]["history_bounded"] is (count > 200)
    saved = freeze(store, path)
    assert rebuild_economic_input(store, path, saved) == expected
    rows = sum(len(json.loads(item.body).get("documents", [])) for item in store.values.values())
    assert rows == min(count, 201)


def test_tradingview_same_time_disagreement_and_cutoffs_are_fixed(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="success"))
        retain_market_capture(c, capture(receipt_id="failure", error=True))
        retain_market_capture(
            c, capture(receipt_id="future", received=EVALUATED + timedelta(seconds=1))
        )
        retain_market_capture(
            c, capture(receipt_id="next-jst-day", received=datetime(2026, 1, 5, 15, tzinfo=UTC))
        )
    expected = current(path)
    assert expected["tradingview_market"]["quotes"][0]["status"] == "ambiguous_same_time"
    saved = freeze(store, path)
    assert rebuild_economic_input(store, path, saved) == expected


def test_latest_matsui_unreadable_document_is_not_discarded(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = MemoryR2Store()
    with closing(initialize_database(path)) as c, c:
        store_document(
            c,
            SourceDocument(
                source_id="matsui-margin",
                request_key="2026-02-05",
                fetched_at=datetime(2026, 2, 5, tzinfo=UTC),
                source_url="https://example.invalid/fixture",
                content_type="text/html",
                payload=b"<html>unreadable</html>",
                metadata={"receipt_jst_date": "2026-02-05"},
            ),
        )
    expected = current(path)
    matsui = next(
        s
        for s in expected["reading"]["series"]
        if s["series_id"] == "jp.matsui_margin_long_evaluation_pnl"
    )
    assert "source_unreadable" in matsui["flags"]
    saved = freeze(store, path)
    assert rebuild_economic_input(store, path, saved) == expected
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("UPDATE economic_source_documents SET metadata='{}'")
    with pytest.raises(ValueError, match="failed integrity"):
        rebuild_economic_input(store, path, saved)


def test_same_rule_stem_different_bytes_is_not_same_recipe(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    rules = tmp_path / DEFAULT_RULES_PATH.name
    rules.write_bytes(DEFAULT_RULES_PATH.read_bytes())
    saved = freeze(store, path, rules_path=rules)
    rules.write_text(rules.read_text() + "\n# changed without renaming\n")
    with pytest.raises(ValueError, match="recipe differs"):
        rebuild_economic_input(store, path, saved, rules_path=rules)


def test_hash_only_legacy_or_tampered_descriptor_is_not_upgraded(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    legacy = InputSnapshot(kind="sqlite_snapshot", reference="old", sha256="a" * 64)
    with pytest.raises(ValueError, match="fixed selection"):
        rebuild_economic_input(store, path, legacy)
    saved = freeze(store, path)
    altered = saved.model_copy(
        update={"descriptor": {"format": "economic-input-v1", "selection_sha256": "b" * 64}}
    )
    with pytest.raises(ValueError, match="digest-matching"):
        rebuild_economic_input(store, path, altered)


def test_missing_or_changed_selection_object_is_an_integrity_failure(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    saved = freeze(store, path)
    key = "fixed-inputs/economic/v1/" + saved.descriptor["selection_sha256"] + ".json"
    store.store_object(key, b"{}")
    with pytest.raises(LakePublishError, match="missing or changed"):
        rebuild_economic_input(store, path, saved)
