"""Synthetic GET wire shared by provider retry and Jobs readback tests."""

import io
import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jquantsapi
import pytest
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from baibai_engine.data_collection.acquisition.native_records import fetch_native_records
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.transport.context import FetchContext
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)

IDS = ("jp.foreign_flows", "jp.topix", "vix")
DAY = date(2026, 7, 24)
START = DAY - timedelta(days=14)
END = DAY + timedelta(days=7)
BASE = datetime(2026, 10, 13, tzinfo=UTC).timestamp()


def payload(series_id, *, day=DAY, value=None):
    if series_id == "credit":
        return f"observation_date,BAMLC0A0CM\n{day.isoformat()},1.2\n".encode()
    if series_id == "vix":
        return f"observation_date,VIXCLS\n{day.isoformat()},{20 if value is None else value}\n".encode()
    if series_id == "jp.topix":
        return {"data": [{"Date": day.isoformat(), "C": 3000 if value is None else value}]}
    row = dict.fromkeys(jquantsapi.constants.EQ_INVESTOR_TYPES_COLUMNS_V2)
    row.update(
        Section="TSEPrime",
        PubDate=(day + timedelta(days=6)).isoformat(),
        StDate=(day - timedelta(days=4)).isoformat(),
        EnDate=day.isoformat(),
        FrgnBal=500 if value is None else value,
    )
    return {"data": [row]}



@pytest.fixture
def wire(monkeypatch):
    class Wire:
        elapsed = 0.0
        calls = []
        waits = []
        reply = None

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.elapsed += seconds

        def send(self, pool, conn, method, url, **kwargs):
            assert method == "GET"
            series_id = (
                "jp.foreign_flows"
                if "investor-types" in url
                else "jp.topix"
                if "topix" in url
                else "credit"
                if parse_qs(urlsplit(url).query).get("id") == ["BAMLC0A0CM"]
                else "vix"
            )
            if series_id not in {"vix", "credit"}:
                assert kwargs["headers"]["x-api-key"] == "synthetic-key"
            self.calls.append((series_id, url))
            status, headers, value = self.reply(series_id, url)
            if hasattr(value, "read"):
                body = value
            else:
                body = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
            return HTTPResponse(
                body=body, status=status, headers=headers, preload_content=False, pool=pool
            )

        def fetch(self, series_id, *, context=None):
            selected_context = self.context if context is None else context
            native = fetch_native_records(
                require_source(load_definitions().by_id()[series_id].source_id),
                start=START,
                end=END,
                context=selected_context,
            )
            return [row for records in native.values() for row in records]

    wire = Wire()
    wire.context = FetchContext()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(jquantsapi.ClientV2, "_load_config", lambda self: {})
    for name in ["jquants.flows", "jquants.indices"]:
        module = __import__(f"baibai_engine.data_collection.providers.{name}", fromlist=["*"])
        monkeypatch.setattr(module, "_read_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(
        HTTPConnectionPool,
        "_make_request",
        lambda pool, conn, method, url, **kwargs: wire.send(pool, conn, method, url, **kwargs),
    )
    monkeypatch.setattr("time.sleep", wire.sleep)
    monkeypatch.setattr("time.monotonic", lambda: wire.elapsed)
    monkeypatch.setattr("time.time", lambda: BASE + wire.elapsed)

    def no_socket(*args, **kwargs):
        pytest.fail("synthetic transport must not open a provider connection")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    yield wire
    wire.context.close()

