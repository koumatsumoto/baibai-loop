from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest
from tests.engine.test_tradingview_collector import payload, stored

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.tradingview import cli
from baibai_engine.market.tradingview.accepted import (
    accept_observations,
    load_observations,
    verify_observations,
)
from baibai_engine.market.tradingview.collector import preflight_snapshot
from baibai_engine.market.tradingview.diagnostics import SourceDataError
from baibai_engine.market.tradingview.observations import COLUMNS, FetchError


def document():
    return {
        "schema_version": 1,
        "source_ref": {
            "library_file_id": "libfile_a",
            "file_id": "file_b",
            "version": 0,
            "sha256": "a" * 64,
        },
        "observation_date_jst": "2026-10-11",
        "columns": COLUMNS,
        "observations": [
            {
                "trial": 21,
                "timing": {"start": "2026-10-11T01:58:14.800Z", "end": "2026-10-11T01:58:46.209Z"},
                "result": payload(["TSE:1000", "TSE:1001"]),
            },
            {
                "trial": 22,
                "timing": {"start": "2026-10-11T01:59:01.211Z", "end": "2026-10-11T01:59:12.736Z"},
                "result": payload(["TSE:1002"]),
            },
        ],
    }


def input_file(tmp_path, value=None):
    raw = json.dumps(value or document(), sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "input.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def market(tmp_path):
    path = tmp_path / "market.sqlite"
    conn = open_connection(path)
    with conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-11',0)")
        conn.execute(
            "INSERT INTO jquants_master_snapshots VALUES ('2026-10-09','1000','IPO','プライム','機械',1)"
        )
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES ('1000','2026-10-09',123)"
        )
    conn.close()
    return path


def test_explicit_weekend_receipts_keep_exact_times_and_other_market_data(tmp_path):
    path = market(tmp_path)
    source, digest = input_file(tmp_path)
    accepted = load_observations(source, sha256=digest)
    report = accept_observations(path, accepted)
    assert report["rows_added"] == report["rows_verified"] == 3
    assert report["coverage_status"] == "partial"
    assert report["source_ref"] == document()["source_ref"]
    assert report["input_sha256"] == digest
    assert report["provider_requests"] == 0
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT snapshot_date,ticker,fetched_at_utc,quote_at_utc,provider_updated_at_utc,estimate_currency FROM tradingview_forecast_snapshots ORDER BY ticker"
        ).fetchall()
        assert rows == [
            ("2026-10-11", "1000", "2026-10-11T01:58:46.209000+00:00", None, None, None),
            ("2026-10-11", "1001", "2026-10-11T01:58:46.209000+00:00", None, None, None),
            ("2026-10-11", "1002", "2026-10-11T01:59:12.736000+00:00", None, None, None),
        ]
        assert conn.execute("SELECT close FROM jquants_daily_bars").fetchall() == [(123,)]
        assert conn.execute("SELECT snapshot_date FROM jquants_master_snapshots").fetchall() == [
            ("2026-10-09",)
        ]
    with pytest.raises(SourceDataError, match="Previous business day"):
        preflight_snapshot(path, accepted.document.observation_date_jst)
    verify_observations(path, accepted, report)
    original = path.read_bytes()
    again = accept_observations(path, accepted)
    assert again["status"] == "already_recorded"
    assert again["rows_added"] == 0
    assert again["existing_equal_rows"] == 3
    assert path.read_bytes() == original


def test_conflict_does_not_append_other_receipts_or_overwrite_existing(tmp_path):
    path = market(tmp_path)
    prior = document()
    prior["observations"] = [prior["observations"][1]]
    prior["observations"][0]["result"]["data"]["TSE:1002"]["close"] = 999
    source, digest = input_file(tmp_path, prior)
    accept_observations(path, load_observations(source, sha256=digest))
    before = stored(path)
    source, digest = input_file(tmp_path)
    with pytest.raises(SourceDataError, match="conflicts"):
        accept_observations(path, load_observations(source, sha256=digest))
    assert stored(path) == before


def test_insert_failure_rolls_back_entire_acceptance(tmp_path):
    path = market(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_last BEFORE INSERT ON tradingview_forecast_snapshots WHEN NEW.ticker='1002' BEGIN SELECT RAISE(ABORT,'private'); END"
        )
    source, digest = input_file(tmp_path)
    with pytest.raises(SourceDataError, match="acceptance failed"):
        accept_observations(path, load_observations(source, sha256=digest))
    assert stored(path) == []


@pytest.mark.parametrize(
    "mutation",
    ["date", "columns", "source", "zone", "order", "overlap", "accounting", "no_timezone"],
)
def test_invalid_receipts_are_rejected_before_store_open(tmp_path, mutation):
    value = document()
    if mutation == "date":
        value["observation_date_jst"] = "2026-10-09"
    if mutation == "columns":
        value["columns"] = list(reversed(COLUMNS))
    if mutation == "source":
        value["source_ref"]["sha256"] = "private-header"
    if mutation == "zone":
        value["observations"][0]["timing"]["end"] = "2026-10-11T10:58:46.209+09:00"
    if mutation == "order":
        value["observations"][0]["timing"]["start"] = "2026-10-11T02:00:00Z"
    if mutation == "overlap":
        value["observations"][1]["result"] = payload(["TSE:1000"])
    if mutation == "accounting":
        value["observations"][1]["result"]["count"] = 50
    if mutation == "no_timezone":
        value["observations"][0]["timing"]["end"] = "2026-10-11T01:58:46.209"
    source, digest = input_file(tmp_path, value)
    with pytest.raises(FetchError):
        load_observations(source, sha256=digest)
    assert not (tmp_path / "market.sqlite").exists()


def test_hash_mismatch_and_missing_store_do_not_create_database(tmp_path):
    source, digest = input_file(tmp_path)
    with pytest.raises(SourceDataError):
        load_observations(source, sha256="f" * 64)
    with pytest.raises(SourceDataError):
        accept_observations(tmp_path / "absent.sqlite", load_observations(source, sha256=digest))
    assert not (tmp_path / "absent.sqlite").exists()


@pytest.mark.parametrize("change", ["accepted_value", "retained_value", "source_ref"])
def test_readback_checks_all_values_and_retained_history(tmp_path, change):
    path = market(tmp_path)
    prior = document()
    prior["observations"] = [prior["observations"][1]]
    prior["observations"][0]["result"] = payload(["TSE:1003"])
    prior["observations"][0]["timing"] = {
        "start": "2026-10-09T07:00:00Z",
        "end": "2026-10-09T07:00:01Z",
    }
    prior["observation_date_jst"] = "2026-10-09"
    source, digest = input_file(tmp_path, prior)
    accept_observations(path, load_observations(source, sha256=digest))
    source, digest = input_file(tmp_path)
    accepted = load_observations(source, sha256=digest)
    report = accept_observations(path, accepted)
    verify_observations(path, accepted, report)
    if change == "source_ref":
        report = copy.deepcopy(report)
        report["source_ref"]["sha256"] = "b" * 64
    else:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE tradingview_forecast_snapshots SET quote_close=777 WHERE ticker=?",
                ("1000" if change == "accepted_value" else "1003",),
            )
    with pytest.raises(SourceDataError, match="readback"):
        verify_observations(path, accepted, report)


def test_cli_acceptance_never_loads_credentials_and_never_echoes_invalid_input(
    tmp_path, monkeypatch, capsys
):
    path = market(tmp_path)
    source, digest = input_file(tmp_path)
    monkeypatch.setattr(
        cli, "credential_storage", lambda *_: pytest.fail("no OAuth for saved receipts")
    )
    assert (
        cli.main(
            [
                "accept-observations",
                "--sqlite",
                str(path),
                "--input",
                str(source),
                "--input-sha256",
                digest,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rows_added"] == 3
    source.write_text("private-token https://private")
    assert (
        cli.main(
            [
                "accept-observations",
                "--sqlite",
                str(path),
                "--input",
                str(source),
                "--input-sha256",
                digest,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "TradingView acquisition failed; category=storage\n"
