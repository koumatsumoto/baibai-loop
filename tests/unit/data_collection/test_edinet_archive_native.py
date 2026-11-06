"""初回ZIPとscopeの独立保存は、L2や最新headへのfallbackを必要としない。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY,
    ArchiveMetadata,
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError

AT = datetime(2026, 11, 5, 12, 34, 56, 123456, tzinfo=UTC)


def archive(doc_id="S100ONE", source_type=1, body=b"retained bytes", at=AT):
    return NativeArchive.from_response(doc_id, source_type, EdinetResponse(at, body))


def owner(tmp_path, store=None):
    store = store or MemoryR2Store()
    writer = EdinetArchiveL1(store, tmp_path)
    writer.initialize()
    return writer, store


def test_explicit_initialization_and_strict_missing_head(tmp_path):
    writer = EdinetArchiveL1(MemoryR2Store(), tmp_path)
    with pytest.raises(LakePublishError, match="initialization"):
        writer.freeze()
    empty = writer.initialize()
    assert empty.archive_keys == frozenset()
    assert empty.scope == FactsScope()
    with pytest.raises(FileExistsError):
        writer.initialize()
    for missing in ("format", "archives", "acquisition_scope"):
        value = empty.value()
        del value[missing]
        with pytest.raises(ValueError, match="Field required"):
            FixedEdinetArchives(writer.store, value)
    value = empty.value()
    del value["acquisition_scope"]["since"]
    with pytest.raises(ValueError, match="Field required"):
        FixedEdinetArchives(writer.store, value)


def test_missing_head_keeps_received_bytes_but_never_initializes_from_them(tmp_path):
    writer = EdinetArchiveL1(MemoryR2Store(), tmp_path)
    original = archive()
    with pytest.raises(LakePublishError, match="disappeared"):
        writer.publish([original])
    assert (tmp_path / (original.metadata.sha256 + ".object")).read_bytes() == original.content
    assert (tmp_path / "pending.json").is_file()
    with pytest.raises(LakePublishError, match="disappeared"):
        writer.resume_work()
    with pytest.raises(LakePublishError, match="resume_work"):
        writer.initialize()
    assert writer.store.values == {}


def test_first_retained_bytes_day_and_type_identity(tmp_path):
    writer, store = owner(tmp_path)
    first = archive()
    fixed = writer.publish([first, archive(source_type=5)])
    assert fixed.archive_keys == frozenset({("S100ONE", 1), ("S100ONE", 5)})
    assert len([key for key in store.put_keys if key != HEAD_KEY]) == 1
    puts = list(store.put_keys)
    assert writer.publish([archive(at=AT + timedelta(days=1))]).identity == fixed.identity
    assert store.put_keys == puts
    assert fixed.metadata("S100ONE", 1) == first.metadata
    assert fixed.read_csv_zip("S100ONE") == first.content
    with pytest.raises(ValueError, match="conflicting bytes"):
        writer.publish([archive(body=b"different")])
    assert writer.freeze().identity == fixed.identity
    assert (tmp_path / "pending.json").exists()
    with pytest.raises(LakePublishError, match="resume_work"):
        writer.publish([])


def test_legacy_day_is_not_promoted_to_a_receipt_timestamp(tmp_path):
    writer, _ = owner(tmp_path)
    item = archive()
    metadata = item.metadata.model_copy(
        update={"stored_on": date(2021, 1, 1), "received_at_utc": None}
    )
    fixed = writer.publish([NativeArchive(metadata, item.content)])
    assert fixed.metadata("S100ONE", 1).stored_on == date(2021, 1, 1)
    assert fixed.metadata("S100ONE", 1).received_at_utc is None
    writer.publish([item])
    assert writer.freeze().identity == fixed.identity


def test_fixed_reader_never_consults_latest_head_or_unrelated_body(tmp_path):
    writer, store = owner(tmp_path)
    fixed = writer.publish([archive()])
    value = fixed.value()
    restored = FixedEdinetArchives(store, value)
    value["archives"].clear()
    assert restored.identity == fixed.identity
    store.get_keys.clear()
    writer.publish([archive("S100TWO", body=b"another")])
    old_body = next(key for key, item in store.values.items() if item.body == b"retained bytes")
    assert old_body not in store.get_keys
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    assert restored.read_xbrl_zip("S100ONE") == b"retained bytes"
    assert store.get_keys == [old_body]
    with pytest.raises(ValueError, match="unavailable"):
        restored.read_xbrl_zip("S100TWO")
    assert store.get_keys == [old_body]


@pytest.mark.parametrize("tamper", ["missing", "body", "metadata"])
def test_missing_or_changed_fixed_bytes_fail_closed(tmp_path, tamper):
    writer, store = owner(tmp_path)
    fixed = writer.publish([archive()])
    key = next(key for key in store.values if key != HEAD_KEY)
    if tamper == "missing":
        del store.values[key]
    elif tamper == "body":
        store.store_object(key, b"changed")
    else:
        store.values[key].metadata["sha256"] = "0" * 64
    with pytest.raises(LakePublishError):
        fixed.read_xbrl_zip("S100ONE")


def test_scope_union_retains_uninitialized_and_earliest_since(tmp_path):
    writer, _ = owner(tmp_path)
    scope = FactsScope(doc_ids=frozenset({"S100ONE"}), legacy_origins=frozenset({"a" * 64}))
    fixed = writer.publish([], scope=scope)
    assert fixed.scope.since is None
    writer.publish([], scope=FactsScope(since=date(2025, 1, 1)))
    current = writer.publish([], scope=FactsScope(date(2026, 1, 1), frozenset({"S100TWO"})))
    assert current.scope == FactsScope(
        date(2025, 1, 1), frozenset({"S100ONE", "S100TWO"}), frozenset({"a" * 64})
    )
    assert fixed.scope == scope
    assert writer.publish([], scope=FactsScope()).identity == current.identity


class FailedStore(MemoryR2Store):
    failure: tuple[str, bool] | None = None

    def put_file(self, key, path, **kwargs):
        fail = self.failure and (key == HEAD_KEY) == (self.failure[0] == "head")
        if fail:
            _, committed = self.failure
            self.failure = None
            if committed:
                super().put_file(key, path, **kwargs)
            raise LakePublishError("lost acknowledgement")
        return super().put_file(key, path, **kwargs)


@pytest.mark.parametrize("target", ["body", "head"])
@pytest.mark.parametrize("committed", [False, True])
def test_failed_or_uncertain_put_keeps_original_and_resumes(tmp_path, target, committed):
    writer, store = owner(tmp_path, FailedStore())
    original = archive()
    store.failure = target, committed
    if committed:
        fixed = writer.publish([original])
    else:
        with pytest.raises(LakePublishError):
            writer.publish([original])
        assert (tmp_path / "pending.json").exists()
        fixed = writer.resume_work()
    assert not (tmp_path / "pending.json").exists()
    assert fixed.metadata("S100ONE", 1) == original.metadata
    assert fixed.read_xbrl_zip("S100ONE") == original.content


def test_competing_head_join_is_explicit_safe_retry(tmp_path):
    writer, store = owner(tmp_path / "first")
    other = EdinetArchiveL1(store, tmp_path / "other")
    put_file = store.put_file
    triggered = False

    def compete(key, path, **kwargs):
        nonlocal triggered
        if key == HEAD_KEY and not triggered:
            triggered = True
            other.publish([archive("S100TWO")], scope=FactsScope(date(2025, 1, 1)))
        return put_file(key, path, **kwargs)

    store.put_file = compete
    with pytest.raises(LakeCASConflict):
        writer.publish([archive()], scope=FactsScope(doc_ids=frozenset({"S100ONE"})))
    fixed = writer.resume_work()
    assert fixed.archive_keys == frozenset({("S100ONE", 1), ("S100TWO", 1)})
    assert fixed.scope == FactsScope(date(2025, 1, 1), frozenset({"S100ONE"}))


def test_lost_canonical_head_cannot_be_recreated_from_partial_work(tmp_path):
    writer, store = owner(tmp_path, FailedStore())
    store.failure = "head", False
    with pytest.raises(LakePublishError):
        writer.publish([archive()])
    del store.values[HEAD_KEY]
    with pytest.raises(LakePublishError, match="disappeared"):
        writer.resume_work()
    assert HEAD_KEY not in store.values


@pytest.mark.parametrize(
    "change",
    [
        {"source_type": True},
        {"source_type": "1"},
        {"source_type": 2},
        {"doc_id": " S100ONE "},
        {"received_at_utc": AT.replace(tzinfo=None)},
        {"stored_on": date(2020, 1, 1)},
        {"sha256": "bad"},
    ],
)
def test_invalid_archive_metadata_is_rejected(change):
    values = archive().metadata.model_dump()
    with pytest.raises(ValueError, match="validation error"):
        ArchiveMetadata(**(values | change))
