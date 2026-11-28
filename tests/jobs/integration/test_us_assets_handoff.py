"""Exercise the real private source CLI from a whole Assets receipt to SIP capture."""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx2 import MockTransport
from tests.jobs.integration.test_alpaca_research_scope import _CLOSE, _FRI, _RECEIVED, _asset
from tests.jobs.integration.test_listing_master_file import _mixed
from tests.jobs.integration.test_us_daily_handoff import _ENV, _PRIVATE_HEADER, _whole
from tests.jobs.integration.test_us_daily_prices import _CALENDAR_ROWS, _payload

from baibai_engine.data_collection.providers.alpaca_assets import ASSETS_URL
from baibai_engine.data_collection.providers.alpaca_http import AlpacaCredentials
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    ALPACA_ASSETS_SOURCE,
    read_local_listing_master,
)
from baibai_jobs.integration import source_handoff, us_assets_handoff, us_daily_handoff
from baibai_jobs.integration.listing_master_current import LISTING_MASTER_CURRENT_KEY


def test_private_cli_uses_full_assets_original_and_same_sip_daily_transport(
    tmp_path, capsys, monkeypatch
):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "not-a-public-output"))
    previous = tmp_path / "previous.json"
    old_body = _mixed().values[LISTING_MASTER_CURRENT_KEY].body
    previous.write_bytes(old_body)
    packets = tmp_path / "assets"
    daily = tmp_path / "daily"
    rows = [
        _asset(1, "AAPL", name="fixture-name-not-a-log"),
        _asset(2, "ETF", "ARCA", tradable=False),
        _asset(3, "OTC3", "OTC"),
    ]
    calls = []

    def provider(request):
        parts = urlsplit(str(request.url))
        calls.append(parts.path)
        assert request.method == "GET"
        if parts.path == "/v2/assets":
            assert str(request.url) == ASSETS_URL
            return _whole(request, rows)
        if parts.path == "/v2/calendar":
            return _whole(request, _CALENDAR_ROWS[:1])
        assert parts.path == "/v2/stocks/bars"
        query = parse_qs(parts.query)
        assert query["symbols"] == ["AAPL,ETF"]
        assert query["feed"] == ["sip"]
        assert query["asof"] == ["-"]
        assert query["adjustment"] == ["raw"]
        assert query["end"] == [_FRI.isoformat()]
        return _whole(request, _payload(["AAPL", "ETF"]))

    def transport(*, trust_env, retries):
        assert (trust_env, retries) == (False, 0)
        return MockTransport(provider)

    for owner in (us_assets_handoff, us_daily_handoff):
        monkeypatch.setattr(owner, "HTTPTransport", transport)
    monkeypatch.setattr(us_assets_handoff, "_now", lambda: _RECEIVED)
    monkeypatch.setattr(us_daily_handoff, "_now", lambda: _CLOSE + timedelta(minutes=1))
    assert (
        source_handoff.main(
            [
                "acquire",
                "--source",
                "alpaca-us-assets",
                "--directory",
                str(packets),
                "--current-listing-master",
                str(previous),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert not output.err
    assert all(
        x not in output.out
        for x in ("fixture-name-not-a-log", "AAPL", "OTC3", *tuple(_ENV.values()), _PRIVATE_HEADER)
    )
    result = json.loads(output.out)
    assert result["assets"] == 3
    assert result["targets"] == 2
    assert result["received_at"] == _RECEIVED.isoformat()
    assert result["excluded"] == {"other_feed": 1}
    assert json.loads((packets / "result.json").read_text()) == result
    body = (packets / "assets.json").read_bytes()
    assert body == json.dumps(rows, separators=(",", ":")).encode()
    assert result["assets_sha256"] == sha256(body).hexdigest()
    pin = packets / "listing-master.json"
    projected = read_local_listing_master(pin, expected_sha256=result["listing_master_sha256"])
    assert projected["JP"] == read_local_listing_master(previous)["JP"]
    assert projected["US"].source == ALPACA_ASSETS_SOURCE

    assert (
        source_handoff.main(
            [
                "acquire",
                "--source",
                "alpaca-us-daily",
                "--directory",
                str(daily),
                "--asof",
                _FRI.isoformat(),
                "--current-listing-master",
                str(pin),
            ]
        )
        == 0
    )
    notice = json.loads(capsys.readouterr().out)
    assert notice["status"] == "complete"
    assert notice["expected"] == notice["received"] == 2
    assert notice["listing_master_sha256"] == result["listing_master_sha256"]
    assert calls == ["/v2/assets", "/v2/calendar", "/v2/stocks/bars"]
    assert previous.read_bytes() == old_body
    assert not (tmp_path / "not-a-public-output").exists()


@pytest.mark.parametrize("command", ["export", "stage", "download", "replay-copy"])
def test_assets_cli_does_not_stage_download_or_replay_to_shared_storage(
    command, tmp_path, capsys, monkeypatch
):
    calls = []
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **kw: calls.append(kw))
    directory = tmp_path / "not-created"
    status = source_handoff.main(
        [
            command,
            "--source",
            "alpaca-us-assets",
            "--directory",
            str(directory),
            "--stage-id",
            "fixture",
            "--asof",
            _FRI.isoformat(),
            "--since",
            _RECEIVED.isoformat(),
        ]
    )
    assert status == 1
    assert "ValueError" in capsys.readouterr().err
    assert calls == []
    assert not directory.exists()


def test_existing_output_is_never_replaced_or_used_for_new_provider_read(tmp_path):
    def unexpected(_request):
        raise AssertionError("must reject an existing output before making a request")

    with pytest.raises(FileExistsError, match="new private directory"):
        us_assets_handoff.acquire_us_assets(
            previous=None,
            directory=tmp_path,
            expected_ny_date=None,
            transport=MockTransport(unexpected),
            credentials=AlpacaCredentials(*_ENV.values()),
            clock=lambda: _RECEIVED,
        )


def test_failed_or_other_day_provider_response_never_writes_private_packet(
    tmp_path, capsys, monkeypatch
):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(us_assets_handoff, "_now", lambda: _RECEIVED)
    for status in (403, 200):
        monkeypatch.setattr(
            us_assets_handoff,
            "HTTPTransport",
            lambda status=status, **_: MockTransport(
                lambda request: _whole(request, [_asset(1, "AAPL")], status=status)
            ),
        )
        directory = tmp_path / "not-created"
        assert (
            source_handoff.main(
                [
                    "acquire",
                    "--source",
                    "alpaca-us-assets",
                    "--directory",
                    str(directory),
                    "--asof",
                    "2026-11-26",
                ]
            )
            == 1
        )
        output = capsys.readouterr()
        assert ("OSError" if status == 403 else "ValueError") in output.err
        assert not output.out
        assert not directory.exists()
