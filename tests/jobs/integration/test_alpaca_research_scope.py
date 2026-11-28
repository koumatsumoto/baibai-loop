"""Connect an actual Alpaca Assets-shaped fixture to the private listing/SIP boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from tests.jobs.integration.test_listing_master_file import _mixed

from baibai_engine.data_collection.providers.alpaca import MAX_SYMBOLS
from baibai_engine.data_collection.providers.alpaca_assets import AlpacaAssetsReceipt
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    ALPACA_ASSETS_SOURCE,
    decode_current_listing_master,
    encode_current_listing_master,
)
from baibai_jobs.integration.listing_master_current import LISTING_MASTER_CURRENT_KEY
from baibai_jobs.integration.us_daily_plan import plan_current_alpaca_research_prices

_FRI = date(2026, 11, 27)
_SAT = date(2026, 11, 28)
_MON = date(2026, 11, 30)
_CLOSE = datetime(2026, 11, 27, 18, tzinfo=UTC)  # 13:00 ET, shortened regular session.
_RECEIVED = _CLOSE - timedelta(minutes=1)


def _asset(number: int, symbol: str, exchange: str = "NASDAQ", **extra: object):
    return {
        "id": str(UUID(int=number)),
        "class": "us_equity",
        "exchange": exchange,
        "symbol": symbol,
        "name": f"{symbol} - Listing",
        "status": "active",
        "tradable": True,
        "attributes": ["has_options"],
        **extra,
    }


def _receipt(*assets: dict[str, object], received_at: datetime = _RECEIVED):
    return AlpacaAssetsReceipt(received_at, json.dumps(assets).encode())


def test_provider_receipt_maps_exact_listings_without_broker_or_product_guesses():
    receipt = _receipt(
        _asset(1, "BRK.B", "NYSE"),
        _asset(2, "ETF", "ARCA", name="Example ETF"),
        _asset(3, "US3", "BATS"),
        _asset(4, "US4", "AMEX", tradable=False),
        _asset(5, "US5", "NYSEARCA"),
        _asset(6, "US6", name=None),
        _asset(7, "OTC7", "OTC"),
        _asset(8, "US8", "FUTURE-MIC"),
        _asset(9, "US 9"),
        _asset(10, "DUP"),
        _asset(11, "DUP", "OTC"),
        _asset(12, "TEXAS", "TXSE"),
    )
    inventory = prepare_alpaca_research_inventory(receipt)
    current = inventory.current
    assert current.slice.session == _FRI
    assert current.source == ALPACA_ASSETS_SOURCE
    assert current.received_count == 12
    assert current.rakuten is current.nasdaq_directories is None
    assert current.alpaca_assets_sha256 == receipt.body_sha256
    assert inventory.observed_by_exchange == {
        "NYSE": 1,
        "ARCA": 1,
        "BATS": 1,
        "AMEX": 1,
        "NYSEARCA": 1,
        "NASDAQ": 3,
        "OTC": 2,
        "FUTURE-MIC": 1,
        "TXSE": 1,
    }
    assert inventory.excluded == {
        "unsupported_venue": 2,
        "other_feed": 2,
        "unmapped_symbol": 1,
        "ambiguous_symbol": 1,
    }
    assert tuple(row.key for row in current.slice.rows) == (
        ("ARCX", "ETF"),
        ("ARCX", "US5"),
        ("BATS", "US3"),
        ("XASE", "US4"),
        ("XNAS", "US6"),
        ("XNYS", "BRK.B"),
    )
    assert {row.issue_type for row in current.slice.rows} == {None}
    assert (
        next(row for row in current.slice.rows if row.key == ("XNAS", "US6")).security_name is None
    )
    assert {row.identity_since for row in current.slice.rows} == {_FRI}
    assert current.slice.rows[-1].instrument_id == f"us:alpaca:{UUID(int=1)}"

    for adjustment in ("raw", "split"):
        plan = plan_current_alpaca_research_prices(
            current,
            cutoff=_CLOSE,
            today_open=True,
            price_session=_FRI,
            price_close_at=_CLOSE,
            adjustment=adjustment,
        )
        assert plan is not None
        assert {target.listing_key for target in plan.targets} == {
            row.key for row in current.slice.rows
        }
        (batch,) = plan.batches
        # Nontradable and not-yet-classified products remain research targets only.
        assert batch.query.symbols == ("BRK.B", "ETF", "US3", "US4", "US5", "US6")
        assert batch.query.feed == "sip"
        assert batch.query.adjustment == adjustment
        assert batch.query.symbol_asof == "-"
        assert batch.session_dates == (_FRI,)


def test_asset_uuid_survives_rename_but_not_reuse_and_old_alias_price_is_not_bound():
    initial = prepare_alpaca_research_inventory(
        _receipt(_asset(1, "SAME"), _asset(2, "OLD"), _asset(3, "REUSE"))
    ).current
    changed = prepare_alpaca_research_inventory(
        _receipt(
            _asset(1, "SAME", name="Updated label, same instrument"),
            _asset(2, "NEW", "NYSE"),
            _asset(4, "REUSE"),
            received_at=_RECEIVED + timedelta(days=3),
        ),
        previous=initial,
    ).current
    original = {row.key[1]: row for row in initial.slice.rows}
    latest = {row.key[1]: row for row in changed.slice.rows}
    assert original["SAME"].instrument_id == latest["SAME"].instrument_id
    assert original["OLD"].instrument_id == latest["NEW"].instrument_id
    assert original["REUSE"].instrument_id != latest["REUSE"].instrument_id
    assert not {"OLD"} & latest.keys()
    assert latest["SAME"].identity_since == _FRI
    assert latest["NEW"].identity_since == latest["REUSE"].identity_since == _MON
    plan = plan_current_alpaca_research_prices(
        changed,
        cutoff=changed.received_at,
        today_open=True,
        price_session=_FRI,
        price_close_at=_CLOSE,
        adjustment="raw",
    )
    assert plan is not None
    assert [target.listing_key for target in plan.targets] == [("XNAS", "SAME")]


def test_same_cutoff_cannot_relabel_old_inventory_or_a_new_weekend_alias():
    current = prepare_alpaca_research_inventory(_receipt(_asset(1, "ACME"))).current
    sat = _RECEIVED + timedelta(days=1)
    new = prepare_alpaca_research_inventory(
        _receipt(_asset(2, "ACME"), received_at=sat), previous=current
    ).current
    assert new.slice.session == _SAT
    assert (
        plan_current_alpaca_research_prices(
            new,
            cutoff=sat,
            today_open=False,
            price_session=_FRI,
            price_close_at=_CLOSE,
            adjustment="raw",
        )
        is None
    )
    prior = plan_current_alpaca_research_prices(
        current,
        cutoff=sat,
        today_open=False,
        price_session=_FRI,
        price_close_at=_CLOSE,
        adjustment="split",
    )
    assert prior is not None
    assert prior.listing_session == _FRI
    assert (
        plan_current_alpaca_research_prices(
            current,
            cutoff=sat + timedelta(days=2),
            today_open=True,
            price_session=_FRI,
            price_close_at=_CLOSE,
            adjustment="raw",
        )
        is None
    )
    assert (
        plan_current_alpaca_research_prices(
            current,
            cutoff=_RECEIVED - timedelta(seconds=1),
            today_open=True,
            price_session=_FRI,
            price_close_at=_CLOSE,
            adjustment="raw",
        )
        is None
    )

    old = _receipt(_asset(1, "ACME"), received_at=_RECEIVED - timedelta(days=1))
    with pytest.raises(ValueError, match="older than the current scope"):
        prepare_alpaca_research_inventory(old, previous=current)
    with pytest.raises(ValueError, match="same receipt clock"):
        prepare_alpaca_research_inventory(_receipt(_asset(2, "ACME")), previous=current)


def test_snapshot_uses_one_current_codec_and_does_not_change_existing_jp_or_enrichment():
    mixed = _mixed().values[LISTING_MASTER_CURRENT_KEY].body
    baseline = decode_current_listing_master(mixed)
    assert encode_current_listing_master(baseline) == mixed
    jp_only = encode_current_listing_master({"JP": baseline["JP"]})
    current = prepare_alpaca_research_inventory(
        _receipt(_asset(1, "ACME")), previous=baseline["US"]
    ).current
    assert current.slice.rows[0].identity_since == _FRI
    assert (
        plan_current_alpaca_research_prices(
            baseline["US"],
            cutoff=_CLOSE,
            today_open=True,
            price_session=_FRI,
            price_close_at=_CLOSE,
            adjustment="raw",
        )
        is None
    )
    entries = decode_current_listing_master(
        encode_current_listing_master({"JP": baseline["JP"], "US": current})
    )
    assert entries["US"] == current
    assert encode_current_listing_master({"JP": entries["JP"]}) == jp_only

    with pytest.raises(ValueError, match="Alpaca scope"):
        replace(current, rakuten=baseline["US"].rakuten)
    with pytest.raises(ValueError, match="Alpaca scope"):
        replace(current, alpaca_assets_sha256=None)
    with pytest.raises(ValueError, match="cannot claim Nasdaq"):
        replace(current, nasdaq_directories=baseline["US"].nasdaq_directories)


def test_full_research_scope_splits_queries_at_vendor_limit():
    receipt = _receipt(*(_asset(i, f"TEST{i:03}") for i in range(1, MAX_SYMBOLS * 2 + 2)))
    current = prepare_alpaca_research_inventory(receipt).current
    plan = plan_current_alpaca_research_prices(
        current,
        cutoff=_CLOSE,
        today_open=True,
        price_session=_FRI,
        price_close_at=_CLOSE,
        adjustment="raw",
    )
    assert plan is not None
    assert [len(batch.query.symbols) for batch in plan.batches] == [MAX_SYMBOLS, MAX_SYMBOLS, 1]
    assert [s for batch in plan.batches for s in batch.query.symbols] == [
        f"TEST{i:03}" for i in range(1, MAX_SYMBOLS * 2 + 2)
    ]


def test_same_day_symbol_reuse_requires_new_uuid_to_be_seen_before_exact_close():
    early = _CLOSE - timedelta(hours=3)
    late = _CLOSE + timedelta(minutes=10)
    original = prepare_alpaca_research_inventory(
        _receipt(_asset(1, "REUSE"), _asset(10, "SAME"), received_at=early)
    ).current
    refreshed = prepare_alpaca_research_inventory(
        _receipt(_asset(2, "REUSE"), _asset(10, "SAME"), received_at=late),
        previous=original,
    ).current
    # Replay the serialized clock, including the early same-day proof across a late refresh.
    current = decode_current_listing_master(encode_current_listing_master({"US": refreshed}))["US"]
    by_symbol = {row.key[1]: row for row in current.slice.rows}
    assert by_symbol["SAME"].identity_observed_at == early
    assert by_symbol["REUSE"].identity_observed_at == late
    plan = plan_current_alpaca_research_prices(
        current,
        cutoff=late,
        today_open=True,
        price_session=_FRI,
        price_close_at=_CLOSE,
        adjustment="raw",
    )
    assert plan is not None
    assert [target.listing_key[1] for target in plan.targets] == ["SAME"]

    mon_close = datetime(2026, 11, 30, 21, tzinfo=UTC)
    monday = prepare_alpaca_research_inventory(
        _receipt(
            _asset(2, "REUSE"), _asset(10, "SAME"), received_at=mon_close - timedelta(hours=1)
        ),
        previous=current,
    ).current
    next_plan = plan_current_alpaca_research_prices(
        monday,
        cutoff=mon_close,
        today_open=True,
        price_session=_MON,
        price_close_at=mon_close,
        adjustment="raw",
    )
    assert next_plan is not None
    assert [target.listing_key[1] for target in next_plan.targets] == ["REUSE", "SAME"]


def test_first_inventory_after_close_does_not_prove_earlier_bar_or_guess_calendar():
    late = _CLOSE + timedelta(minutes=10)
    current = prepare_alpaca_research_inventory(
        _receipt(_asset(1, "ACME"), received_at=late)
    ).current
    for close in (_CLOSE, None, _CLOSE + timedelta(days=1)):
        assert (
            plan_current_alpaca_research_prices(
                current,
                cutoff=late,
                today_open=True,
                price_session=_FRI,
                price_close_at=close,
                adjustment="raw",
            )
            is None
        )
