from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import TypeAdapter
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.jquants_listing import normalize_jp_listings
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingSlice,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    masters_from_payloads,
    validate_master_snapshot,
)

_DAY = date(2026, 11, 27)
_RECEIVED = datetime(2026, 11, 27, 8, 43, tzinfo=UTC)


def _page(rows, *, query=None, cursor=None, received_at=_RECEIVED):
    payload = {"data": rows}
    if cursor is not None:
        payload["pagination_key"] = cursor
    return JQuantsPage(
        "master",
        {"date": _DAY.isoformat()} if query is None else query,
        received_at,
        json.dumps(payload).encode(),
    )


def test_two_pages_preserve_each_issue_and_legacy_current_projection():
    rows = make_master_records(_DAY)
    rows[0].update(
        Code="12a40",
        CoName="合成商事",
        Mkt="0112",
        MktNm="スタンダード",
        S33="7100",
        S33Nm="証券･商品先物取引業",
    )
    rows.extend(
        (
            dict(rows[0], Code="12A45", CoName="合成商事 優先"),
            dict(rows[0], Code="12A46", CoName="合成商事 第2優先"),
            dict(rows[0], Code="12A41", CoName="合成商事 新株"),
            dict(rows[0], Code="90000", CoName="合成ETF", Mkt="0109", S33="9999", ProdCat="014"),
            dict(rows[0], Code="90010", CoName="合成REIT", ProdCat="013"),
            dict(rows[0], Code="90020", CoName="合成外国株", ProdCat="021"),
            dict(rows[0], Code="90030", CoName="合成優先出資証券", ProdCat="012"),
            dict(rows[0], Code="90040", CoName="合成外国預託証券", ProdCat="024"),
            dict(rows[0], Code="90050", CoName="未分類（合成）", ProdCat="opaque-new-code"),
        )
    )
    received_at = _RECEIVED + timedelta(seconds=2)
    capture = normalize_jp_listings(
        [
            _page(rows[:1500], cursor="next"),
            _page(
                rows[1500:],
                query={"date": _DAY.isoformat(), "pagination_key": "next"},
                received_at=received_at,
            ),
        ],
        session=_DAY,
    )
    assert capture.session == _DAY
    assert capture.received_at == received_at
    assert len(capture.observations) == len(rows)
    assert {row.key[0] for row in capture.observations} == {"XTKS"}
    observed = {row.key[1]: row for row in capture.observations}
    actual_types = tuple(
        observed[code].issue_type
        for code in (
            "12A40",
            "12A45",
            "12A46",
            "12A41",
            "90000",
            "90010",
            "90020",
            "90030",
            "90040",
            "90050",
        )
    )
    assert actual_types == (
        "common_stock",
        "preferred_stock",
        "preferred_stock",
        "domestic_secondary_issue",
        "etf",
        "reit",
        "foreign_stock",
        "preferred_investment_security",
        "depositary_receipt",
        None,
    )
    assert observed["12A40"].japan == JapanListingAttributes(
        "011", "スタンダード", "証券・商品先物取引業", "0112", "7100"
    )
    assert observed["12A45"].japan == observed["12A40"].japan
    assert observed["90050"].japan is not None
    assert observed["90050"].japan.product_category == "opaque-new-code"
    assert observed["10010"].japan is not None
    assert observed["10010"].japan.market_segment_code is None
    assert observed["10010"].japan.sector_33_code is None

    snapshot = validate_master_snapshot(rows, _DAY)
    legacy = masters_from_payloads([payload for _, _, payload in snapshot.rows])
    new_primary = {code[:4]: row for code, row in observed.items() if code.endswith("0")}
    assert set(new_primary) == {security.ticker for security in legacy}
    for security in legacy:
        row = new_primary[security.ticker]
        assert row.japan is not None
        assert row.security_name == security.name
        assert row.japan.market_segment == security.market_segment
        assert row.japan.sector_33 == security.sector_33
        assert (row.issue_type == "common_stock") == security.is_common_stock


def test_a_metadata_correction_is_current_state_not_a_new_listing_event():
    rows = make_master_records(_DAY)
    rows[0].update(S33="7100", S33Nm="証券・商品先物取引業", Mkt="0112", MktNm="スタンダード")
    before = normalize_jp_listings([_page(rows)], session=_DAY).observations
    base = refresh_listing_slice(
        None, market="JP", session=_DAY, observations=before, complete=True
    )
    rows[0].update(S33="7200", S33Nm="その他金融業", Mkt="0111", MktNm="プライム")
    after = normalize_jp_listings([_page(rows)], session=_DAY).observations
    updated = refresh_listing_slice(
        base.slice, market="JP", session=_DAY, observations=after, complete=True
    )
    assert updated.slice.input_fingerprint != base.slice.input_fingerprint
    assert updated.changes == ()
    old = base.slice.rows[0]
    new = updated.slice.rows[0]
    assert new.instrument_id == old.instrument_id
    assert new.listing_state == "listed"
    assert new.effective_session is None
    assert new.japan == JapanListingAttributes("011", "プライム", "その他金融業", "0111", "7200")
    codec = TypeAdapter(ListingSlice)
    assert codec.validate_json(codec.dump_json(updated.slice)) == updated.slice

    absent = refresh_listing_slice(
        updated.slice,
        market="JP",
        session=_DAY + timedelta(days=3),
        observations=after[1:],
        complete=True,
    )
    missing = absent.slice.rows[0]
    assert missing.key == new.key
    assert missing.listing_state == "unverified"
    assert missing.instrument_id == new.instrument_id
    assert tuple(change.kind for change in absent.changes) == ("unverified",)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("empty", "empty J-Quants"),
        ("short_population", "production floor"),
        ("wrong_requested_date", "date-only master request"),
        ("single_code", "date-only master request"),
        ("holiday_forwards", "Date does not match"),
        ("duplicate_issue", "duplicate .* security code"),
        ("nonstr_sector_code", "string S33"),
    ],
)
def test_rejects_noncurrent_or_partial_semantic_capture(case, message):
    rows = make_master_records(_DAY)
    query = {"date": _DAY.isoformat()}
    if case == "empty":
        rows.clear()
    if case == "short_population":
        rows.pop()
    if case == "wrong_requested_date":
        query["date"] = "2026-11-26"
    if case == "single_code":
        query["code"] = "10000"
    if case == "holiday_forwards":
        rows[0]["Date"] = "2026-11-30"
    if case == "duplicate_issue":
        rows.append(dict(rows[0], Code=" " + rows[0]["Code"].lower() + " "))
    if case == "nonstr_sector_code":
        rows[0]["S33"] = 7100
    with pytest.raises(JQuantsProviderError, match=message):
        normalize_jp_listings([_page(rows, query=query)], session=_DAY)


@pytest.mark.parametrize("case", ["missing_next", "jumped_cursor", "wrong_source"])
def test_checks_native_response_scope_and_full_chain_even_above_population_floor(case):
    rows = make_master_records(_DAY)
    first = _page(rows, cursor="genuine")
    if case == "missing_next":
        pages = (first,)
    elif case == "jumped_cursor":
        pages = (
            first,
            _page([], query={"date": _DAY.isoformat(), "pagination_key": "invented"}),
        )
    else:
        with pytest.raises(JQuantsProviderError, match="date-only master request"):
            normalize_jp_listings([replace(_page(rows), source="daily_bars")], session=_DAY)
        return
    with pytest.raises(JQuantsAcquisitionError, match=r"incomplete|pagination identity"):
        normalize_jp_listings(pages, session=_DAY)
