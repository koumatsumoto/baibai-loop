"""Independent financial L1 publication, retries and source-native change semantics."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage, complete_request
from baibai_engine.data_collection.providers.jquants.financial_native import (
    HEAD_KEY,
    PREFIX,
    FinancialAttempt,
    FinancialL1,
    financial_content_id,
)
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error

DAY = date(2026, 11, 1)
AT = datetime(2026, 11, 2, tzinfo=UTC)


def attempt(identity, value=1, *, at=AT, day=DAY, partial=False, no_pages=False):
    body = {"data": [{"Code": "13010", "DiscDate": day.isoformat(), "DiscNo": "1", "Sales": value}]}
    if partial:
        body["pagination_key"] = "next"
    pages = (
        ()
        if no_pages
        else (
            JQuantsPage(
                "financial_summaries", {"date": day.isoformat()}, at, json.dumps(body).encode()
            ),
        )
    )
    return FinancialAttempt(
        identity, day, at, pages, "interrupted" if partial or no_pages else None
    )


@pytest.fixture
def owner(tmp_path):
    result = FinancialL1(MemoryR2Store(), tmp_path)
    result.initialize()
    return result


def packs(owner):
    return [key for key in owner.store.values if key.startswith(PREFIX + "packs/")]


def test_complete_request_equivalence_preserves_values_unknowns_and_duplicates():
    a = attempt("a")
    first = a.pages[0]
    records = json.loads(first.body)["data"]
    paged = complete_request(
        (
            replace(first, body=json.dumps({"data": [], "pagination_key": "x"}).encode()),
            replace(
                first,
                query={**first.query, "pagination_key": "x"},
                body=json.dumps({"data": records}).encode(),
            ),
        )
    )
    assert financial_content_id(complete_request(a.pages)) == financial_content_id(paged)
    for extra in (None, 0, "", False):
        changed = replace(
            first, body=json.dumps({"data": [{**records[0], "Unknown": extra}]}).encode()
        )
        assert financial_content_id(complete_request((changed,))) != financial_content_id(paged)
    duplicate = replace(first, body=json.dumps({"data": records + records}).encode())
    assert financial_content_id(complete_request((duplicate,))) != financial_content_id(paged)
    extra_envelope = replace(first, body=json.dumps({"data": records, "Unknown": None}).encode())
    assert financial_content_id(complete_request((extra_envelope,))) != financial_content_id(paged)


def test_equal_changes_and_return_to_prior_content(owner):
    assert owner.commit([attempt("a")])["changed"] == ["a"]
    first = owner.fixed()
    first_packs = packs(owner)
    assert owner.commit([attempt("a2", at=AT + timedelta(hours=1))])["equal"] == ["a2"]
    assert packs(owner) == first_packs
    state = owner.fixed().value()["scopes"][DAY.isoformat()]
    assert state["adopted"] == "a"
    assert state["confirmed_at"] > state["changed_at"]
    assert owner.commit([attempt("b", 2, at=AT + timedelta(hours=2))])["changed"] == ["b"]
    assert owner.commit([attempt("a3", at=AT + timedelta(hours=3))])["changed"] == ["a3"]
    assert len(packs(owner)) == 3
    assert [item.request_id for item in first.requests(owner.store, days=[DAY])] == ["a"]
    assert [item.request_id for item in owner.fixed().requests(owner.store, days=[DAY])] == ["a3"]
    before = len(owner.store.put_keys)
    assert owner.commit([attempt("a3", at=AT + timedelta(hours=3))])["retried"] == ["a3"]
    assert len(owner.store.put_keys) == before
    with pytest.raises(ValueError, match="identity conflicts"):
        owner.commit([attempt("a3", 5, at=AT + timedelta(hours=3))])


@pytest.mark.parametrize("no_pages", [False, True])
def test_failure_then_equal_success_updates_plan_and_fixed_incomplete(owner, no_pages):
    owner.commit([attempt("a")])
    failed = attempt("f", at=AT + timedelta(hours=1), partial=not no_pages, no_pages=no_pages)
    assert owner.commit([failed])["failed"] == ["f"]
    fixed_failure = owner.fixed()
    end = DAY + timedelta(days=10)
    assert DAY in fixed_failure.plan(start=DAY, end=end, revision_overlap_days=0)
    assert [r.request_id for r in fixed_failure.requests(owner.store, days=[DAY])] == ["a", "f"]
    count = len(packs(owner))
    assert owner.commit([attempt("equal", at=AT + timedelta(hours=2))])["equal"] == ["equal"]
    assert len(packs(owner)) == count
    assert DAY not in owner.fixed().plan(start=DAY, end=end, revision_overlap_days=0)
    assert [r.request_id for r in owner.fixed().requests(owner.store, days=[DAY])] == ["a"]
    assert [r.request_id for r in fixed_failure.requests(owner.store, days=[DAY])] == ["a", "f"]


def test_late_and_same_clock_attempts_do_not_roll_back(owner):
    owner.commit([attempt("new", 2, at=AT + timedelta(hours=2))])
    assert owner.commit([attempt("late", 1)])["held"] == ["late"]
    assert owner.commit([attempt("same", 3, at=AT + timedelta(hours=2))])["held"] == ["same"]
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"] == "new"
    # The business period is old, but this is a genuinely newer receipt.
    assert owner.commit([attempt("correction", 4, at=AT + timedelta(hours=3))])["changed"] == [
        "correction"
    ]


def test_normal_empty_is_a_completed_replacement(owner):
    owner.commit([attempt("a")])
    empty = attempt("empty", at=AT + timedelta(hours=1))
    empty = replace(empty, pages=(replace(empty.pages[0], body=b'{"data": []}'),))
    assert owner.commit([empty])["changed"] == ["empty"]
    assert next(owner.fixed().requests(owner.store, days=[DAY])).request.records() == []


class FailPacks(MemoryR2Store):
    fail = True
    uploaded = False

    def put_file(self, key, path, **kwargs):
        if "/packs/" in key and self.fail:
            if self.uploaded:
                super().put_file(key, path, **kwargs)
            raise R2Error("lost acknowledgement")
        return super().put_file(key, path, **kwargs)


def test_pending_write_blocks_new_plan_and_recovers_original_identity(tmp_path):
    store = FailPacks()
    owner = FinancialL1(store, tmp_path / "work")
    owner.initialize()
    with pytest.raises(R2Error):
        owner.commit([attempt("a")])
    fixed = owner.fixed()
    assert fixed.value()["receipts"] == {}
    assert fixed.value()["pending"] is not None
    with pytest.raises(LakePublishError, match="pending"):
        fixed.plan(start=DAY, end=DAY, revision_overlap_days=0)
    assert next(fixed.requests(store, days=[DAY])).request is None
    fresh = FinancialL1(store, tmp_path / "fresh")
    with pytest.raises(LakePublishError, match="saved work directory"):
        fresh.resume()
    store.fail = False
    owner.resume()
    assert [r.request_id for r in owner.fixed().requests(store, days=[DAY])] == ["a"]
    assert owner.commit([attempt("a")])["retried"] == ["a"]


def test_lost_put_response_is_verified_without_new_request(tmp_path):
    store = FailPacks()
    store.uploaded = True
    owner = FinancialL1(store, tmp_path)
    owner.initialize()
    assert owner.commit([attempt("a")])["changed"] == ["a"]
    assert owner.fixed().value()["pending"] is None
    assert len(packs(owner)) == 1


def test_packs_are_bounded_and_no_single_request_is_truncated(owner, monkeypatch):
    import baibai_engine.data_collection.providers.jquants.financial_native as native

    monkeypatch.setattr(native, "MAX_PACK_BYTES", 600)
    owner.commit([attempt("a"), attempt("b", day=DAY + timedelta(days=1))])
    assert len(packs(owner)) == 2
    before = owner.fixed().content_id
    with pytest.raises(ValueError, match="exceeds pack limit"):
        owner.commit([attempt("big", "x" * 2000, at=AT + timedelta(hours=1))])
    assert owner.fixed().content_id == before


def test_missing_or_corrupt_head_is_not_an_empty_source(owner):
    owner.commit([attempt("a")])
    del owner.store.values[HEAD_KEY]
    with pytest.raises(LakePublishError, match="not initialized"):
        owner.fixed()


def test_filtered_query_is_not_a_full_day(owner):
    item = attempt("a")
    filtered = replace(
        item, pages=(replace(item.pages[0], query={"date": DAY.isoformat(), "code": "13010"}),)
    )
    with pytest.raises(ValueError, match="unfiltered"):
        owner.commit([filtered])


def test_older_equal_confirmation_retry_never_stores_a_body(owner):
    owner.commit([attempt("a")])
    equal = attempt("a2", at=AT + timedelta(hours=1))
    owner.commit([equal])
    owner.commit([attempt("a3", at=AT + timedelta(hours=2))])
    before = len(packs(owner))
    assert owner.commit([equal])["retried"] == ["a2"]
    assert len(packs(owner)) == before
    assert "pack" not in owner.fixed().value()["receipts"]["a2"]
    with pytest.raises(ValueError, match="identity conflicts"):
        owner.commit([replace(equal, failure="changed metadata")])


def test_lost_head_cannot_be_reinitialized_over_retained_originals(owner):
    owner.commit([attempt("a")])
    del owner.store.values[HEAD_KEY]
    with pytest.raises(ValueError, match="restore its head"):
        owner.initialize()


def test_success_older_than_known_failure_is_held_until_recheck(owner):
    owner.commit([attempt("f", at=AT + timedelta(hours=1), partial=True)])
    assert owner.commit([attempt("old", at=AT)])["held"] == ["old"]
    assert [item.request_id for item in owner.fixed().requests(owner.store, days=[DAY])] == ["f"]
    assert owner.commit([attempt("fresh", at=AT + timedelta(hours=2))])["changed"] == ["fresh"]


def test_same_clock_conflict_is_fixed_as_missing_and_planned_for_recheck(owner):
    owner.commit([attempt("a")])
    owner.commit([attempt("conflict", 2)])
    fixed = owner.fixed()
    assert DAY in fixed.plan(start=DAY, end=DAY + timedelta(days=10), revision_overlap_days=0)
    requests = list(fixed.requests(owner.store, days=[DAY]))
    assert [item.request_id for item in requests] == ["a", "conflict"]
    assert requests[0].request is not None
    assert requests[1].request is None
    owner.commit([attempt("confirmed", at=AT + timedelta(hours=1))])
    assert DAY not in owner.fixed().plan(
        start=DAY, end=DAY + timedelta(days=10), revision_overlap_days=0
    )
    assert len(list(owner.fixed().requests(owner.store, days=[DAY]))) == 1


def test_migrated_receipt_preserves_unknown_start_without_fabrication(owner):
    migrated = replace(attempt("old"), attempted_at=None)
    raw = migrated.value()
    assert raw["attempted_at"] is None
    assert FinancialAttempt.from_value(raw) == migrated
    assert owner.commit([migrated])["changed"] == ["old"]
    fixed = owner.fixed()
    assert fixed.value()["scopes"][DAY.isoformat()]["changed_at"] == AT.isoformat()
    retained = next(fixed.requests(owner.store, days=[DAY]))
    assert retained.request.received_at == AT
    assert owner.commit([migrated])["retried"] == ["old"]
    with pytest.raises(ValueError, match="without pages"):
        replace(migrated, pages=())
