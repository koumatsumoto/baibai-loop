from contextlib import closing
from datetime import timedelta

import pytest
from pydantic import ValidationError
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


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "invalid", ["symbols", "duplicates", "clock", "naive_clock", "tool", "bar_count"]
)
def test_mutated_capture_metadata_cannot_commit_an_unreadable_receipt(tmp_path, existing, invalid):
    path = tmp_path / "native.sqlite"
    changed = capture(bars=invalid == "bar_count")
    if invalid == "symbols":
        changed.request_parameters["symbols"].clear()
    elif invalid == "duplicates":
        changed.request_parameters["symbols"] *= 2
    elif invalid == "clock":
        changed = changed.model_copy(update={"requested_at_utc": CLOCK + timedelta(seconds=1)})
    elif invalid == "naive_clock":
        changed = changed.model_copy(update={"requested_at_utc": CLOCK.replace(tzinfo=None)})
    elif invalid == "tool":
        changed = changed.model_copy(update={"tool_name": "unsupported-tool"})
    else:
        changed.request_parameters["count"] = True
    prior = capture(receipt_id="prior")
    with closing(initialize_database(path)) as conn:
        if existing:
            retain_market_capture(conn, prior)
        before = path.read_bytes()
        statements = []
        conn.set_trace_callback(statements.append)
        with pytest.raises(ValidationError):
            retain_market_capture(conn, changed)
        assert statements == []
        assert not conn.in_transaction
        assert path.read_bytes() == before
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == int(
            existing
        )
        if existing:
            assert (
                read_market_capture(conn, prior.receipt_id, expected_sha256=prior.digest()) == prior
            )


@pytest.mark.parametrize("bars", [False, True])
@pytest.mark.parametrize("error", [False, True])
def test_valid_capture_copy_preserves_native_bytes_digest_and_retry(tmp_path, bars, error):
    path = tmp_path / "native.sqlite"
    original = capture(bars=bars, error=error).model_copy(
        update={"receipt_id": "valid-copy", "requested_at_utc": CLOCK - timedelta(seconds=3)}
    )
    native = original.result_bytes()
    digest = original.digest()
    with closing(initialize_database(path)) as conn:
        assert retain_market_capture(conn, original)
        before = path.read_bytes()
        assert not retain_market_capture(conn, original)
        assert path.read_bytes() == before
        restored = read_market_capture(conn, original.receipt_id, expected_sha256=digest)
        assert restored == original
        assert restored.result_bytes() == native
        assert restored.digest() == digest


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
    assert quote["observations"][0]["status"] == "received"
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


@pytest.mark.parametrize("field", ["count", "missing_count"])
@pytest.mark.parametrize("invalid", [0, True, 1.0])
def test_market_batch_uses_native_response_counts(field, invalid):
    value = capture().model_dump(by_alias=True)
    value["result"]["structuredContent"][field] = invalid  # one quote and one explicit missing
    with pytest.raises(ValueError, match="response_count_mismatch"):
        read_market_observation(MarketToolCapture.model_validate(value))


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


@pytest.mark.parametrize(("size", "invalid"), [(0, False), (1, True), (1, 1.0)])
def test_bar_count_must_be_an_integer_even_when_row_count_compares_equal(size, invalid):
    value = capture(bars=True).model_dump(by_alias=True)
    payload = value["result"]["structuredContent"]
    payload["bars"] = payload["bars"][:size]
    payload["count"] = size
    assert len(read_market_observation(MarketToolCapture.model_validate(value))["bars"]) == size
    payload["count"] = invalid
    with pytest.raises(ValueError, match="exact request"):
        read_market_observation(MarketToolCapture.model_validate(value))
