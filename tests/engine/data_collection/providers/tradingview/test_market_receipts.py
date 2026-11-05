from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.tradingview_market import CLOCK, capture

from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture
from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    read_market_capture,
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    read_market_observation,
)


def test_native_sdk_and_request_parameters_survive_idempotent_storage(tmp_path):
    original = capture(bars=True)
    with closing(initialize_database(tmp_path / "native.sqlite")) as conn:
        assert retain_market_capture(conn, original)
        assert not retain_market_capture(conn, original)
        restored = read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
        assert restored == original
        assert restored.result_bytes() == original.result_bytes()
        assert restored.request_parameters == original.request_parameters
        assert "request_parameters" not in restored.result.model_dump(by_alias=True)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 0


def test_same_receipt_cannot_change_its_clock_or_payload(tmp_path):
    original = capture()
    with closing(initialize_database(tmp_path / "native.sqlite")) as conn:
        retain_market_capture(conn, original)
        for changed in (
            capture(received=CLOCK + timedelta(minutes=1)),
            capture(error=True),
        ):
            with pytest.raises(ValueError, match="conflict"):
                retain_market_capture(conn, changed)
            assert (
                read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
                == original
            )


def test_new_reception_of_same_values_is_not_discarded(tmp_path):
    original = capture()
    later = capture(receipt_id="later", received=CLOCK + timedelta(minutes=1))
    assert original.result_bytes() == later.result_bytes()
    with closing(initialize_database(tmp_path / "native.sqlite")) as conn:
        assert retain_market_capture(conn, original)
        assert retain_market_capture(conn, later)
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 2
        assert (
            read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
            == original
        )
        with pytest.raises(ValueError, match="digest differs"):
            read_market_capture(conn, later.receipt_id, expected_sha256=original.digest())


def test_native_tool_error_is_retained_and_not_a_zero_price(tmp_path):
    original = capture(error=True)
    with closing(initialize_database(tmp_path / "native.sqlite")) as conn:
        retain_market_capture(conn, original)
        restored = read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
    view = read_market_observation(restored)
    assert view["status"] == "failed"
    assert view["observations"] == []
    assert original.result_bytes() == restored.result_bytes()


def test_delay_missing_and_bar_clock_are_not_realtime_or_trade_clock():
    quote = read_market_observation(capture())
    assert quote["formal_proposal_price"] is False
    assert quote["observations"][0]["delay_seconds"] == 600
    assert quote["observations"][0]["fields"]["change"] == 0
    assert quote["observations"][0]["last_trade_at"] is None
    assert quote["observations"][1] == {"symbol": "TSE:SYNTH", "status": "missing"}
    bars = read_market_observation(capture(bars=True))
    assert bars["last_bar_may_change"] is True
    assert bars["bars"][0]["v"] == 0
    assert bars["bars"][1]["v"] is None
    assert bars["update_mode"] is None
    assert bars["last_trade_at"] is None
    assert bars["last_bar_at"] != bars["available_at"]


@pytest.mark.parametrize("count", [0, 501, True, "2"])
def test_explicit_finite_bar_request_rejects_invalid_count(count):
    value = capture(bars=True).model_dump()
    value["request_parameters"]["count"] = count
    with pytest.raises(ValueError, match=r"count 1\.\.500"):
        MarketToolCapture.model_validate(value)


def test_request_clock_and_nonfinite_result_are_rejected():
    value = capture().model_dump()
    value["requested_at_utc"] = CLOCK + timedelta(seconds=1)
    with pytest.raises(ValueError, match="request follows"):
        MarketToolCapture.model_validate(value)
    value = capture().model_dump(by_alias=True)
    value["result"]["structuredContent"]["data"]["CME:SYNTH"]["close"] = float("nan")
    with pytest.raises(ValueError, match="Out of range float"):
        MarketToolCapture.model_validate(value)


def test_bad_bar_schema_is_retained_before_projection_failure(tmp_path):
    value = capture(bars=True).model_dump(by_alias=True)
    value["result"]["structuredContent"]["symbol"] = "OTHER:SYNTH"
    original = MarketToolCapture.model_validate(value)
    with closing(initialize_database(tmp_path / "native.sqlite")) as conn:
        assert retain_market_capture(conn, original)
        restored = read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
        with pytest.raises(ValueError, match="exact request"):
            read_market_observation(restored)
        assert (
            read_market_capture(conn, original.receipt_id, expected_sha256=original.digest())
            == original
        )
