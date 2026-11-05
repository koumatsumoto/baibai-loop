"""Master shares explicit-date persistence, not price/financial acquisition policy."""

import json
from datetime import UTC, date, datetime, timedelta

from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import (
    PREFIX,
    MasterAttempt,
    MasterL1,
    plan_master_days,
)

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, tzinfo=UTC)


def attempt(identity, records, *, hours=0, query=None):
    at = AT + timedelta(hours=hours)
    return MasterAttempt(
        identity,
        DAY,
        at,
        (
            JQuantsPage(
                "master",
                query or {"date": DAY.strftime("%Y%m%d")},
                at,
                json.dumps({"data": records, "extra": "keep"}).encode(),
            ),
        ),
    )


def test_master_raw_scope_keeps_all_products_unknown_fields_and_corrections(tmp_path):
    store = MemoryR2Store()
    owner = MasterL1(store, tmp_path)
    owner.initialize()
    records = [
        {"Code": "13010", "ProdCat": "011", "unused": {"x": 1}},
        {"Code": "13011", "ProdCat": "011"},
        {"Code": "130A0", "ProdCat": "ETF"},
    ]
    assert owner.commit([attempt("first", records)])["changed"] == ["first"]
    before = owner.fixed()
    assert plan_master_days(store, before, start=DAY, end=DAY) == ()
    assert owner.commit([attempt("equal", records, hours=1)])["equal"] == ["equal"]
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == 1
    assert owner.commit([attempt("corrected", records[1:], hours=2)])["changed"] == ["corrected"]
    assert next(before.requests(store, days=[DAY])).request.records() == records
    assert next(owner.fixed().requests(store, days=[DAY])).request.records() == records[1:]


def test_empty_is_retained_but_not_a_fulfilled_master_and_sparse_dates_are_not_backfilled(tmp_path):
    store = MemoryR2Store()
    owner = MasterL1(store, tmp_path)
    owner.initialize()
    owner.commit([attempt("empty", [])])
    assert next(owner.fixed().requests(store, days=[DAY])).request.records() == []
    assert plan_master_days(store, owner.fixed(), start=DAY, end=DAY) == (DAY,)
    owner.commit([attempt("small-but-nonempty", [{"Code": "13010"}], hours=1)])
    assert plan_master_days(store, owner.fixed(), start=DAY, end=DAY) == ()
    assert plan_master_days(
        store, owner.fixed(), start=DAY + timedelta(days=4), end=DAY + timedelta(days=4)
    ) == (DAY + timedelta(days=4),)
    assert set(owner.fixed().value()["scopes"]) == {DAY.isoformat()}


def test_filtered_master_cannot_establish_full_population(tmp_path):
    import pytest

    owner = MasterL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    with pytest.raises(ValueError, match="unfiltered"):
        owner.commit(
            [
                attempt(
                    "filtered",
                    [{"Code": "13010"}],
                    query={"date": DAY.isoformat(), "code": "13010"},
                )
            ]
        )
    assert owner.fixed().value()["scopes"] == {}
