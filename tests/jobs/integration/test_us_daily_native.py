"""Exercise the native day through the actual typed collector."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_alpaca_research_scope import _CLOSE, _FRI, _SAT, _asset, _receipt
from tests.jobs.integration.test_us_daily_prices import (
    _CALENDAR_ROWS,
    _calendar,
    _payload,
    _response,
)

from baibai_engine.data_collection.providers.alpaca import MAX_SYMBOLS
from baibai_engine.data_collection.providers.alpaca_sip_native import PREFIX, read_alpaca_sip_day
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_jobs.integration.us_daily_native import write_alpaca_sip_day
from baibai_jobs.integration.us_daily_prices import collect_current_us_daily_prices

_KEY = f"{PREFIX}{_FRI}.json"
_ATTEMPT = UUID(int=1)


def _collect(*, failure=None, missing="", cutoff=None, scope=None, price=10.5):
    cutoff = cutoff or _CLOSE + timedelta(minutes=20)
    if scope is None:
        scope = prepare_alpaca_research_inventory(
            _receipt(*(_asset(n + 1, f"S{n:02d}") for n in range(MAX_SYMBOLS * 2 + 1)))
        ).current

    def request(url):
        query = parse_qs(urlsplit(url).query)
        assert query["feed"] == ["sip"]
        assert query["adjustment"] == ["raw"]
        assert query["start"] == query["end"] == [_FRI.isoformat()]
        symbols = query["symbols"][0].split(",")
        if failure and (failure == "all" or symbols[0] == "S20"):
            return _response({}, status=403 if failure == "all" else 503, arrived=cutoff)
        body = _payload(symbol for symbol in symbols if symbol != missing)
        for rows in body["bars"].values():
            rows[0] = {**rows[0], "c": price}
        return _response(body, arrived=cutoff)

    result = collect_current_us_daily_prices(
        scope,
        _calendar(arrived=cutoff, rows=_CALENDAR_ROWS[:1], end=_FRI),
        attempt_id=_ATTEMPT,
        clock=lambda: cutoff,
        transport=request,
        expected_price_session=_FRI,
    )
    assert result is not None
    return result


def test_native_session_retains_real_short_close_and_partial_retry_never_erases_rows():
    store = MemoryR2Store()
    initial = _collect(failure="batch", missing="S00")
    saved = write_alpaca_sip_day(store, initial)
    assert saved is not None
    assert saved.day.closed_at == _CLOSE
    assert saved.day.session == _FRI
    assert saved.day.expected == 2 * MAX_SYMBOLS + 1
    assert len(saved.day.bars) == MAX_SYMBOLS
    assert saved.day.missing == (UUID(int=1),)
    assert saved.day.unverified == tuple(UUID(int=n) for n in range(21, 41))
    bar = saved.day.bars[0]
    assert bar.instrument_id == f"us:alpaca:{UUID(int=2)}"
    assert (bar.symbol, bar.mic, bar.close, bar.volume) == ("S01", "XNAS", Decimal("10.5"), 42)
    assert (saved.day.feed, saved.day.adjustment, saved.day.price_unit) == (
        "sip",
        "raw",
        "USD_per_share",
    )
    assert saved.sha256 == sha256(store.values[_KEY].body).hexdigest()
    assert read_alpaca_sip_day(store, session=_FRI) == saved
    assert read_alpaca_sip_day(store, session=_SAT) is None
    assert write_alpaca_sip_day(store, initial) == saved
    assert write_alpaca_sip_day(store, _collect(failure="all")) == saved
    assert store.put_keys == [_KEY]

    retry = _collect(missing="S40", cutoff=_CLOSE + timedelta(minutes=25), price=10.8)
    merged = write_alpaca_sip_day(store, retry)
    assert merged is not None
    assert merged.day.expected == len(merged.day.bars) == MAX_SYMBOLS * 2 + 1
    assert not merged.day.missing
    assert not merged.day.unverified
    assert merged.day.bars[-1] == saved.day.bars[-1]  # Not returned on retry, kept.
    assert merged.day.bars[0].close == Decimal("10.8")
    assert merged.sha256 != saved.sha256
    assert store.put_keys == [_KEY, _KEY]
    assert tuple(store.values) == (_KEY,)
    assert write_alpaca_sip_day(store, initial) == merged  # Old prices cannot replace new ones.


def test_all_unverified_or_different_listing_evidence_does_not_replace_day():
    store = MemoryR2Store()
    assert write_alpaca_sip_day(store, _collect(failure="all")) is None
    assert not store.put_keys
    initial = _collect()
    saved = write_alpaca_sip_day(store, initial)
    scope = prepare_alpaca_research_inventory(_receipt(_asset(1, "NEW"))).current
    with pytest.raises(ValueError, match="different close or asset scope"):
        write_alpaca_sip_day(store, _collect(scope=scope))
    assert read_alpaca_sip_day(store, session=_FRI) == saved
    assert store.put_keys == [_KEY]
    before_close = replace(initial, session_close_at=_CLOSE + timedelta(hours=1))
    with pytest.raises(ValueError, match="post-close receipts"):
        write_alpaca_sip_day(store, before_close)


def test_conditional_publish_recovers_committed_response_loss_but_not_concurrent_change():
    class LostResponse(MemoryR2Store):
        def put_file(self, *args, **options):
            super().put_file(*args, **options)
            raise LakePublishError("fixture connection closed after the write")

    source = _collect()
    store = LostResponse()
    saved = write_alpaca_sip_day(store, source)
    assert read_alpaca_sip_day(store, session=_FRI) == saved
    assert store.put_keys == [_KEY]

    conflicting = MemoryR2Store()
    competing = _collect(cutoff=_CLOSE + timedelta(hours=1), price=10.9)
    opponent = write_alpaca_sip_day(conflicting, competing)
    competitor_body = conflicting.values[_KEY].body

    class Competitor(MemoryR2Store):
        def put_file(self, key, *args, **kwargs):
            self.store_object(key, competitor_body)
            return super().put_file(key, *args, **kwargs)

    replacing = Competitor()
    with pytest.raises(LakeCASConflict):
        write_alpaca_sip_day(replacing, source)
    assert read_alpaca_sip_day(replacing, session=_FRI) == opponent
    assert replacing.put_keys == [_KEY]
