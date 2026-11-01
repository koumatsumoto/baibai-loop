from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import baibai_jobs.acquisition.tradingview.cooldown as cooldown
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
)

NOW = datetime(2026, 10, 13, 7, tzinfo=UTC)


class Store:
    def __init__(self):
        self.body = None
        self.etag = "0"
        self.writes = 0
        self.fault = None

    def head(self, key):
        assert key == cooldown.KEY
        if self.fault == "read":
            raise LakePublishError("private")
        return (
            None
            if self.body is None
            else RemoteObject(self.etag, len(self.body), {}, "application/json")
        )

    def get_bytes(self, key):
        assert key == cooldown.KEY
        body = self.body
        if self.fault == "generation":
            self.etag += "1"
        return body

    def put_file(self, key, path, **kwargs):
        assert key == cooldown.KEY
        self.writes += 1
        if self.fault == "before":
            raise LakePublishError("private")
        assert kwargs["if_none_match"] == (self.body is None)
        assert kwargs["if_match"] == (None if self.body is None else self.etag)
        self.body = path.read_bytes()
        self.etag += "1"
        if self.fault == "after":
            raise LakePublishError("private")
        if self.fault == "conflict":
            value = json.loads(self.body)
            value["not_before_utc"] = (
                cooldown.timestamp(value["not_before_utc"]) + timedelta(hours=1)
            ).isoformat()
            self.body = json.dumps(value).encode()
            raise LakeCASConflict("private")
        return self.head(key)


@pytest.mark.parametrize("hours", [None, -1, 0, 5])
def test_missing_expired_and_future_deadline(tmp_path, monkeypatch, capsys, hours):
    store = Store()
    if hours is not None:
        cooldown.extend(store, cooldown.KEY, NOW + timedelta(hours=hours))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(cooldown, "datetime", Clock)
    monkeypatch.setattr(cooldown, "Boto3R2Store", lambda **_: store)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cooldown.main(["check", "--directory", str(tmp_path)]) == 0
    assert f"allowed={'false' if hours == 5 else 'true'}" in output.read_text()
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize("fault", [None, "after", "conflict"])
def test_extension_is_monotonic_and_recovers_ambiguous_write(fault):
    store = Store()
    store.fault = fault
    cooldown.extend(store, cooldown.KEY, NOW + timedelta(hours=5))
    actual, _ = cooldown.read_deadline(store, cooldown.KEY)
    assert actual >= NOW + timedelta(hours=5)
    writes = store.writes
    cooldown.extend(store, cooldown.KEY, NOW + timedelta(hours=1))
    assert store.writes == writes


@pytest.mark.parametrize("fault", ["before", "read"])
def test_storage_failure_is_not_a_successful_record(fault):
    store = Store()
    store.fault = fault
    with pytest.raises((cooldown.CooldownError, LakePublishError)):
        cooldown.extend(store, cooldown.KEY, NOW)


@pytest.mark.parametrize(
    "body",
    [
        b"bad",
        b"{}",
        b'{"not_before_utc":"2026-10-13"}',
        b'{"not_before_utc":"2026-10-13T09:00:00+09:00"}',
    ],
)
def test_corrupt_state_stops_acquisition(body):
    store = Store()
    store.body = body
    with pytest.raises(cooldown.CooldownError):
        cooldown.read_deadline(store, cooldown.KEY)


def test_generation_change_stops_acquisition():
    store = Store()
    cooldown.extend(store, cooldown.KEY, NOW)
    store.fault = "generation"
    with pytest.raises(cooldown.CooldownError):
        cooldown.read_deadline(store, cooldown.KEY)


def test_partial_and_failure_record_only_the_latest_safe_deadline(tmp_path):
    prefix = "TradingView acquisition failed; category=provider_rate_limit; retry_not_before_utc="
    (tmp_path / "tradingview-stderr.log").write_text(prefix + NOW.isoformat() + "\n")
    (tmp_path / "tradingview-result.json").write_text(
        json.dumps(
            {"status": "partial", "last_failure": prefix + (NOW + timedelta(hours=5)).isoformat()}
        )
    )
    assert cooldown.reported_deadline(tmp_path) == NOW + timedelta(hours=5)


def test_no_wait_instruction_does_not_create_state_or_connect(tmp_path, monkeypatch):
    (tmp_path / "tradingview-stderr.log").write_text(
        "echo 'TradingView acquisition failed; category=auth; retry_not_before_utc=2099-01-01T00:00:00Z'\n"
    )
    monkeypatch.setattr(cooldown, "Boto3R2Store", lambda **_: pytest.fail("unnecessary R2 access"))
    assert cooldown.main(["record", "--directory", str(tmp_path)]) == 0
