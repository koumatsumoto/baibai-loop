from __future__ import annotations

import json
import signal
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_engine.data_collection.equity_web.capture import (
    EquityWebCapture,
    load_capture,
    save_capture,
)
from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.data_collection.external_assessment import cli, collection
from baibai_engine.machine_analysis.observations.external_assessments.normalization import ParseError, monex
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    ModelValuationObservation,
)
from baibai_engine.storage.observations.schema import open_connection


class Session:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, html = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response


class BytesSession(Session):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, body, headers = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response._content = body
        response._content_consumed = True
        response.headers.update(headers)
        return response


def _original(*, attempt_id=None, body=None):
    return EquityWebSourceResponse(
        receipt_id=str(uuid.uuid4()),
        attempt_id=attempt_id or str(uuid.uuid4()),
        source="monex",
        requested_ticker="7203",
        role="page",
        parent_receipt_id=None,
        requested_url=monex.public_url("7203"),
        received_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
        selected_encoding="utf-8",
        body=body if body is not None else monex_html().encode(),
    )


@pytest.mark.parametrize("status", [302, 500])
def test_http_redirect_and_server_error_are_failures_before_null_parsing(status, monkeypatch):
    client = BytesSession(
        [(status, monex_html(value="--").encode(), {"Location": "https://private.example/secret"})]
    )

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("HTTP failure must not reach the observation parser")

    monkeypatch.setattr(monex, "parse_html", unexpected_parse)
    result = collection.collect(["7203"], sources=("monex",), session=client)
    assert not result.observations
    assert result.requests == 1
    assert len(client.calls) == 1
    assert client.calls[0][1]["allow_redirects"] is False
    assert result.outcomes[0].status == "http_failure"
    assert result.outcomes[0].http_status == status
    assert result.outcomes[0].attempt_id is not None
    assert result.summary()["outcome_counts"] == {"http_failure": 1}
    assert "secret" not in json.dumps(result.summary())


@pytest.mark.parametrize(
    ("content_type", "meta", "encoding"),
    [
        ("text/html; charset=Shift_JIS", '<meta charset="utf-8">', "shift_jis"),
        ("text/html; charset='EUC-JP'", '<meta charset="utf-8">', "euc_jp"),
        ("text/html", '<meta charset="Shift_JIS">', "shift_jis"),
        (None, '<meta charset="EUC-JP">', "euc_jp"),
    ],
)
def test_explicit_header_and_meta_non_utf8_decode_without_guessing(content_type, meta, encoding):
    body = (meta + monex_html(value="--")).encode(encoding)
    headers = {} if content_type is None else {"Content-Type": content_type}
    client = BytesSession([(200, body, headers)])
    before = datetime.now(UTC)
    result = collection.collect(["7203"], sources=("monex",), session=client)
    after = datetime.now(UTC)
    assert result.outcomes[0].status == "observed"
    assert len(result.observations) == 1
    assert result.observations[0].consensus_target is None
    assert before <= result.observations[0].fetched_at_utc <= after
    assert result.observations[0].fetched_at_utc.tzinfo is UTC
    assert result.requests == 1


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "text/html; charset=private-secret-charset",
            ('<meta charset="utf-8">' + monex_html(value="--")).encode(),
        ),
        (
            "text/html",
            ('<meta charset="private-secret-charset">' + monex_html(value="--")).encode(),
        ),
        ("text/html; charset=utf-8", b"\xff private-secret-body"),
        ("text/html; charset=Shift_JIS", b"\x80 private-secret-body"),
        ("text/html", b'<meta charset="EUC-JP">\xff private-secret-body'),
        (None, b"\xff private-secret-body"),
    ],
)
def test_invalid_bytes_and_unknown_charset_are_fixed_failures_not_null(content_type, body):
    headers = {} if content_type is None else {"Content-Type": content_type}
    client = BytesSession([(200, body, headers)])
    result = collection.collect(["7203"], sources=("monex",), session=client)
    assert not result.observations
    assert result.requests == 1
    assert result.outcomes[0].status == "parse_failure"
    assert result.outcomes[0].reason == "response_encoding"
    assert result.outcomes[0].http_status == 200
    assert result.outcomes[0].attempt_id is not None
    assert result.summary()["outcome_counts"] == {"parse_failure": 1}
    assert "private-secret" not in json.dumps(result.summary())


def test_live_collector_separates_acquisition_and_receipt_time(monkeypatch):
    fake_clock(monkeypatch)
    client = Session([(200, monex_html()), (200, official_html()), (200, rakuten_html())])
    before = datetime.now(UTC)
    result = collection.collect(["7203"], session=client)
    after = datetime.now(UTC)
    assert len(result.observations) == 2
    assert result.requests == 3
    assert result.stop_reason is None
    assert all(before <= row.fetched_at_utc <= after for row in result.observations)
    assert client.calls[-1][0].endswith("bcode=7203.T")
    assert all(call[1] == {"timeout": 10, "allow_redirects": False} for call in client.calls)


@pytest.mark.parametrize(("status", "category"), [(403, "access_denied"), (429, "rate_limited")])
def test_limit_response_stops_only_that_source(status, category, monkeypatch):
    fake_clock(monkeypatch)
    client = Session(
        [
            (status, "secret"),
            (200, official_html()),
            (200, rakuten_html()),
            (200, official_html("9432")),
            (200, rakuten_html("9432")),
        ]
    )
    result = collection.collect(["7203", "9432"], session=client)
    assert result.requests == 5
    assert len(result.observations) == 2
    assert [row.status for row in result.outcomes] == [
        category,
        "not_attempted",
        "observed",
        "observed",
    ]
    assert result.outcomes[0].http_status == status
    assert result.outcomes[1].http_status is None
    assert result.outcomes[1].reason == category
    assert sum("monex.co.jp" in url for url, _ in client.calls) == 1
    assert "secret" not in json.dumps(result.summary())


def test_iframe_403_stops_remaining_rakuten_tickers(monkeypatch):
    fake_clock(monkeypatch)
    client = Session([(200, official_html()), (403, "secret")])
    result = collection.collect(["7203", "9432"], sources=("rakuten",), session=client)
    assert result.requests == 2
    assert not result.observations
    assert result.outcomes[0].status == "access_denied"


def test_budget_counts_official_and_iframe_separately(monkeypatch):
    fake_clock(monkeypatch)
    client = Session([(200, monex_html()), (200, official_html())])
    result = collection.collect(["7203"], session=client, request_budget=2)
    assert result.requests == 2
    assert result.stop_reason == "request_budget"
    assert result.outcomes[-1].status == "incomplete"
    assert result.outcomes[-1].reason == "request_budget"
    assert len(result.observations) == 1


def test_actual_wall_clock_interrupts_a_transport_that_ignores_timeout(monkeypatch):
    def stall(*args, **kwargs):
        time.sleep(5)
        raise AssertionError("deadline did not interrupt transport")

    client = Session([])
    monkeypatch.setattr(client, "get", stall)
    start = time.monotonic()
    result = collection.collect(["7203"], session=client, time_limit_seconds=0.05)
    assert time.monotonic() - start < 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.requests == 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_timeout_and_shell_are_failures_not_nullable_observations(monkeypatch):
    client = Session([(200, "<html>secret shell</html>")])
    result = collection.collect(["7203"], session=client, sources=("monex",))
    assert not result.observations
    assert result.outcomes[0].status == "parse_failure"
    assert result.outcomes[0].http_status == 200
    assert result.outcomes[0].reason == "ticker_identity"
    monkeypatch.setattr(
        client, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("secret"))
    )
    result = collection.collect(["7203"], session=client, sources=("monex",))
    assert result.outcomes[0].status == "request_timeout"
    assert "secret" not in json.dumps(result.summary())


def test_no_forecast_shell_has_safe_identity_failure_and_no_observation(monkeypatch):
    fake_clock(monkeypatch)
    client = Session(
        [
            (200, official_html("130A")),
            (200, "<p>この銘柄のアナリスト業績予測情報はありません。</p>"),
        ]
    )
    result = collection.collect(["130A"], sources=("rakuten",), session=client)
    assert not result.observations
    assert result.outcomes[0].status == "parse_failure"
    assert result.outcomes[0].reason == "no_estimates_identity_unconfirmed"
    assert result.outcomes[0].http_status == 200
    assert "アナリスト" not in json.dumps(result.summary(), ensure_ascii=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tickers": []},
        {"tickers": ["7203"] * 2},
        {"tickers": ["7203", "9432", "6758", "8306"]},
        {"request_budget": 10},
        {"request_budget": 0},
        {"time_limit_seconds": float("nan")},
        {"request_timeout_seconds": 31},
    ],
)
def test_invalid_expansion_never_calls_transport(kwargs):
    client = Session([])
    with pytest.raises(ValueError, match=r"requires|budget|limit|timeout"):
        collection.collect(session=client, **kwargs)
    assert not client.calls


def test_private_capture_roundtrip_preserves_original_receipt(tmp_path):
    original = _original(body=monex_html(value="--").encode())
    path = tmp_path / "private.json"
    save_capture(path, EquityWebCapture((original,), (original.attempt_id,)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    replay = load_capture(path)
    assert replay.responses == (original,)
    assert replay.selected_attempt_ids == (original.attempt_id,)
    assert "consensus_target" not in path.read_text()


def test_cli_collect_captures_only_and_import_requires_explicit_db(monkeypatch, tmp_path, capsys):
    original = _original()
    observed = monex.parse_html(
        monex_html(),
        ticker="7203",
        source_url=monex.public_url("7203"),
        fetched_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    result = collection.CollectionResult(
        (observed,),
        (
            collection.CollectionOutcome(
                "monex", "7203", "observed", http_status=200, attempt_id=original.attempt_id
            ),
        ),
        1,
    )

    def collect(*args, on_received_html, **kwargs):
        on_received_html(original)
        return result

    monkeypatch.setattr(collection, "collect", collect)
    path = tmp_path / "private.json"
    assert cli.main(["collect", "--output", str(path)]) == 0
    assert load_capture(path).responses == (original,)
    with pytest.raises(SystemExit):
        cli.main(["import", "--input", str(path)])
    db = tmp_path / "copy.sqlite"
    open_connection(db).close()
    assert cli.main(["import", "--input", str(path), "--db", str(db)]) == 0
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (1,)
    assert cli.main(["import", "--input", str(path), "--db", str(db)]) == 0
    assert "2024" not in capsys.readouterr().out


def test_cli_import_reports_storage_failure_without_values_or_http(monkeypatch, tmp_path, capsys):
    def fail(*args):
        raise ValueError("secret licensed numeric data")

    from baibai_engine.data_collection.equity_web import capture

    monkeypatch.setattr(capture, "load_capture", fail)
    monkeypatch.setattr(
        collection, "collect", lambda *args, **kwargs: pytest.fail("import accessed provider")
    )
    assert (
        cli.main(
            [
                "import",
                "--input",
                str(tmp_path / "stage.json"),
                "--db",
                str(tmp_path / "copy.sqlite"),
            ]
        )
        == 1
    )
    assert "secret" not in capsys.readouterr().err


def test_cli_import_rejects_missing_or_unmigrated_db_without_bootstrap(
    monkeypatch, tmp_path, capsys
):
    original = _original()
    stage = tmp_path / "stage.json"
    save_capture(stage, EquityWebCapture((original,), (original.attempt_id,)))
    missing = tmp_path / "missing.sqlite"
    assert cli.main(["import", "--input", str(stage), "--db", str(missing)]) == 1
    assert not missing.exists()
    assert json.loads(capsys.readouterr().err)["category"] == "storage_failure"
    old = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(old)) as conn, conn:
        conn.execute("CREATE TABLE old_store (value INTEGER)")
    assert cli.main(["import", "--input", str(stage), "--db", str(old)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "schema_failure"
    with closing(sqlite3.connect(old)) as conn, conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("old_store",)
        ]


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def fake_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(collection.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(collection.time, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("parse_seconds", [0, 1, 2, 3])
def test_minimum_interval_waits_only_remaining_time_after_response(monkeypatch, parse_seconds):
    clock = fake_clock(monkeypatch)
    client = Session([(200, monex_html()), (200, monex_html("9432"))])
    starts = []
    original_get = client.get
    original_parse = monex.parse_html

    def get(*args, **kwargs):
        starts.append(clock.now)
        return original_get(*args, **kwargs)

    def parse(*args, **kwargs):
        clock.now += parse_seconds
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(client, "get", get)
    monkeypatch.setattr(monex, "parse_html", parse)
    result = collection.collect(["7203", "9432"], sources=("monex",), session=client)
    assert result.requests == 2
    assert len(result.observations) == 2
    assert starts == [100, 100 + max(parse_seconds, 2)]
    assert clock.sleeps == ([2 - parse_seconds] if parse_seconds < 2 else [])


def test_interval_is_after_response_completion_and_across_sources(monkeypatch):
    clock = fake_clock(monkeypatch)
    client = Session([(403, "denied"), (200, official_html()), (200, rakuten_html())])
    starts = []
    original_get = client.get

    def get(*args, **kwargs):
        starts.append(clock.now)
        clock.now += 3
        return original_get(*args, **kwargs)

    monkeypatch.setattr(client, "get", get)
    result = collection.collect(["7203"], session=client)
    assert result.requests == 3
    assert len(result.observations) == 1
    assert starts == [100, 105, 110]
    assert clock.sleeps == [2, 2]


def test_request_budget_stops_before_interval_wait(monkeypatch):
    clock = fake_clock(monkeypatch)
    client = Session([(200, monex_html())])
    result = collection.collect(
        ["7203", "9432"], sources=("monex",), session=client, request_budget=1
    )
    assert result.requests == 1
    assert result.stop_reason == "request_budget"
    assert clock.sleeps == []
    assert result.outcomes[-1].status == "not_attempted"


def test_monotonic_deadline_during_interval_does_not_start_next_http(monkeypatch):
    clock = fake_clock(monkeypatch)
    client = Session([(200, monex_html())])
    result = collection.collect(
        ["7203", "9432"], sources=("monex",), session=client, time_limit_seconds=1
    )
    assert clock.sleeps == [1]
    assert result.requests == 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.outcomes[-1].status == "not_attempted"
    assert result.outcomes[-1].reason == "wall_clock_limit"
    assert len(result.observations) == 1


def test_real_wall_clock_deadline_interrupts_interval_sleep():
    client = Session([(200, monex_html())])
    started = time.monotonic()
    result = collection.collect(
        ["7203", "9432"], sources=("monex",), session=client, time_limit_seconds=0.05
    )
    assert time.monotonic() - started < 1
    assert result.requests == 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.outcomes[-1].status == "not_attempted"
    assert len(result.observations) == 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


@pytest.mark.parametrize("destination", ["file", "directory", "symlink", "dangling_symlink"])
def test_cli_existing_output_is_rejected_before_any_http(
    monkeypatch, tmp_path, capsys, destination
):
    output = tmp_path / "stage.json"
    if destination == "file":
        output.write_text("earlier private observation")
    elif destination == "directory":
        output.mkdir()
    else:
        target = tmp_path / "target.json"
        if destination == "symlink":
            target.write_text("earlier private observation")
        output.symlink_to(target)
    monkeypatch.setattr(
        collection,
        "collect",
        lambda *args, **kwargs: pytest.fail("existing stage accessed provider"),
    )
    assert cli.main(["collect", "--output", str(output)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "staging_destination_exists"
    assert output.exists() or output.is_symlink()


def test_partial_stage_survives_all_failed_second_collection(monkeypatch, tmp_path, capsys):
    original = _original()
    observed = monex.parse_html(
        monex_html(),
        ticker="7203",
        source_url=monex.public_url("7203"),
        fetched_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    partial = collection.CollectionResult(
        (observed,),
        (
            collection.CollectionOutcome(
                "monex", "7203", "observed", http_status=200, attempt_id=original.attempt_id
            ),
            collection.CollectionOutcome(
                "rakuten", "7203", "parse_failure", "missing_rating_section", 200
            ),
        ),
        3,
    )
    calls = []

    def collect_once(*args, on_received_html, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            on_received_html(original)
            return partial
        return collection.CollectionResult(
            (), (collection.CollectionOutcome("monex", "7203", "http_failure", http_status=500),), 1
        )

    monkeypatch.setattr(collection, "collect", collect_once)
    output = tmp_path / "stage.json"
    assert cli.main(["collect", "--output", str(output)]) == 1
    captured_bytes = output.read_bytes()
    assert cli.main(["collect", "--output", str(output)]) == 1
    assert len(calls) == 1
    assert output.read_bytes() == captured_bytes
    assert load_capture(output) == EquityWebCapture((original,), (original.attempt_id,))
    assert json.loads(capsys.readouterr().err)["category"] == "staging_destination_exists"


def test_save_capture_never_overwrites_existing_payload_and_removes_temporary(tmp_path):
    output = tmp_path / "stage.json"
    output.write_bytes(b"original private stage")
    original_paths = set(tmp_path.iterdir())
    with pytest.raises(FileExistsError):
        save_capture(output, EquityWebCapture((), ()))
    assert output.read_bytes() == b"original private stage"
    assert set(tmp_path.iterdir()) == original_paths


def test_save_capture_atomic_nooverwrite_rejects_destination_created_during_save(
    monkeypatch, tmp_path
):
    output = tmp_path / "stage.json"
    original_link = Path.hardlink_to
    original_paths = set(tmp_path.iterdir())

    def raced_link(path, target):
        path.write_bytes(b"concurrent original private stage")
        return original_link(path, target)

    monkeypatch.setattr(Path, "hardlink_to", raced_link)
    with pytest.raises(FileExistsError):
        save_capture(output, EquityWebCapture((), ()))
    assert output.read_bytes() == b"concurrent original private stage"
    assert set(tmp_path.iterdir()) == original_paths | {output}


@pytest.mark.parametrize("message", ["changed_theory_label", "secret private HTML numeric payload"])
def test_parse_failure_summary_allows_only_fixed_codes(monkeypatch, message):
    client = Session([(200, monex_html())])

    def fail(*args, **kwargs):
        raise ParseError(message)

    monkeypatch.setattr(monex, "parse_html", fail)
    result = collection.collect(["7203"], sources=("monex",), session=client)
    expected = (
        "changed_theory_label"
        if message == "changed_theory_label"
        else "unclassified_parse_failure"
    )
    assert result.outcomes[0].reason == expected
    assert result.outcomes[0].http_status == 200
    assert "secret" not in json.dumps(result.summary())


def test_validation_failure_summary_has_generic_fixed_code(monkeypatch):
    client = Session([(200, monex_html())])

    def fail(*args, **kwargs):
        ModelValuationObservation.model_validate({"private": "secret response"})

    monkeypatch.setattr(monex, "parse_html", fail)
    result = collection.collect(["7203"], sources=("monex",), session=client)
    assert result.outcomes[0].reason == "observation_contract"
    assert result.outcomes[0].http_status == 200
    assert "secret" not in json.dumps(result.summary())
