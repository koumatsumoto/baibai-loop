"""Daily bars share persistence mechanics, not another source's scope or values."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage, complete_request
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.data_collection.providers.jquants.price_native import (
    PREFIX,
    PriceAttempt,
    PriceL1,
    price_content_id,
)

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def attempt(identity, close=100, *, at=AT, records=None):
    rows = [{"Code": "13010", "Date": DAY.isoformat(), "C": close}] if records is None else records
    return PriceAttempt(
        identity,
        DAY,
        at,
        (
            JQuantsPage(
                "daily_bars", {"date": DAY.isoformat()}, at, json.dumps({"data": rows}).encode()
            ),
        ),
    )


def test_prices_and_financials_keep_independent_namespaces_and_source_validation(tmp_path):
    store = MemoryR2Store()
    prices = PriceL1(store, tmp_path / "price")
    finance = FinancialL1(store, tmp_path / "finance")
    prices.initialize()
    finance.initialize()
    price = attempt("same-id")
    financial = FinancialAttempt(
        "same-id", DAY, AT, (replace(price.pages[0], source="financial_summaries"),)
    )
    prices.commit([price])
    finance.commit([financial])
    assert prices.fixed().content_id != finance.fixed().content_id
    assert next(prices.fixed().requests(store, days=[DAY])).request.source == "daily_bars"
    with pytest.raises(ValueError, match="unexpected source"):
        PriceAttempt("wrong", DAY, AT, financial.pages)
    with pytest.raises(ValueError, match="unfiltered"):
        prices.commit(
            [
                replace(
                    price,
                    request_id="filtered",
                    pages=(
                        replace(price.pages[0], query={"date": DAY.isoformat(), "code": "13010"}),
                    ),
                )
            ]
        )


def test_price_confirmations_changes_and_empty_withdrawal(tmp_path):
    owner = PriceL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    owner.commit([attempt("first")])
    fixed = owner.fixed()
    owner.commit([attempt("equal", at=AT + timedelta(hours=1))])
    assert len([key for key in owner.store.values if key.startswith(PREFIX + "packs/")]) == 1
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"] == "first"
    assert owner.commit([attempt("withdrawn", at=AT + timedelta(hours=2), records=[])])[
        "changed"
    ] == ["withdrawn"]
    assert next(owner.fixed().requests(owner.store, days=[DAY])).request.records() == []
    assert next(fixed.requests(owner.store, days=[DAY])).request.records()[0]["C"] == 100


def test_price_native_preserves_unknown_fields_and_pagination_equivalence():
    original = attempt("a").pages[0]
    rows = json.loads(original.body)["data"]
    paged = complete_request(
        (
            replace(original, body=b'{"data": [], "pagination_key": "x"}'),
            replace(
                original,
                query={**original.query, "pagination_key": "x"},
                body=json.dumps({"data": rows}).encode(),
            ),
        )
    )
    assert price_content_id(complete_request((original,))) == price_content_id(paged)
    for value in (None, 0, False, ""):
        changed = replace(
            original, body=json.dumps({"data": [{**rows[0], "Unknown": value}]}).encode()
        )
        assert price_content_id(complete_request((changed,))) != price_content_id(paged)


def test_price_partial_is_retained_and_replanned_without_a_projection(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path)
    owner.initialize()
    owner.commit([attempt("old")])
    partial = replace(
        attempt("partial", at=AT + timedelta(hours=1)),
        pages=(
            replace(
                attempt("x").pages[0],
                received_at=AT + timedelta(hours=1),
                body=b'{"data": [], "pagination_key": "next"}',
            ),
        ),
        failure="interrupted",
    )
    assert owner.commit([partial])["failed"] == ["partial"]
    fixed = owner.fixed()
    assert DAY in fixed.plan(start=DAY, end=DAY + timedelta(days=10), revision_overlap_days=0)
    assert [
        (item.request_id, item.error is not None) for item in fixed.requests(store, days=[DAY])
    ] == [("old", False), ("partial", True)]
