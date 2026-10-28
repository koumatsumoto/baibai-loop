"""Manual acquisition retains canonical bytes and imports only selected attempts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.engine.test_external_assessment_collection import BytesSession, _original
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html
from tests.engine.test_kabumap import html as kabumap_html
from tests.jobs.external_assessment_publication_fixtures import pair_responses

from baibai_engine.data_collection.equity_web import http as source_http
from baibai_engine.data_collection.equity_web.capture import (
    EquityWebCapture,
    LegacyCaptureError,
    load_capture,
    save_capture,
)
from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.data_collection.equity_web.receipts import read_responses
from baibai_engine.data_collection.external_assessment import cli, collection
from baibai_engine.machine_analysis.models.external_assessment.normalization import rakuten
from baibai_engine.machine_analysis.models.external_assessment.observations.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.storage.observations.schema import open_connection


def _counts(db):
    with closing(sqlite3.connect(db)) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE, QUANTS_SCORE_TABLE)
        )


def test_three_media_manual_collect_preserves_bytes_then_imports_without_network(
    monkeypatch, tmp_path, capsys
):
    bodies = (
        monex_html(value="--").encode(),
        official_html().encode(),
        rakuten_html().encode(),
        kabumap_html("7203").encode("shift_jis"),
    )
    client = BytesSession(
        [
            (200, body, {"Content-Type": "text/html; charset=" + encoding})
            for body, encoding in zip(bodies, ("utf-8", "utf-8", "utf-8", "shift_jis"), strict=True)
        ]
    )
    original_collect = collection.collect
    monkeypatch.setattr(source_http, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        collection,
        "collect",
        lambda *args, **kwargs: original_collect(*args, session=client, **kwargs),
    )
    output = tmp_path / "capture.json"
    before = datetime.now(UTC)
    assert (
        cli.main(
            [
                "collect",
                "--tickers",
                "7203",
                "--source",
                "monex",
                "--source",
                "rakuten",
                "--source",
                "kabumap",
                "--request-budget",
                "4",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    after = datetime.now(UTC)
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome_counts"] == {"observed": 3}
    capture = load_capture(output)
    assert tuple(response.body for response in capture.responses) == bodies
    assert tuple(response.role for response in capture.responses) == (
        "page",
        "page",
        "iframe",
        "page",
    )
    assert tuple(response.selected_encoding.lower() for response in capture.responses) == (
        "utf-8",
        "utf-8",
        "utf-8",
        "shift_jis",
    )
    assert all(before <= response.received_at_utc <= after for response in capture.responses)
    assert capture.responses[1].attempt_id == capture.responses[2].attempt_id
    assert capture.responses[2].parent_receipt_id == capture.responses[1].receipt_id
    assert capture.responses[0].parent_receipt_id is None
    assert capture.selected_attempt_ids == (
        capture.responses[0].attempt_id,
        capture.responses[1].attempt_id,
        capture.responses[3].attempt_id,
    )
    assert all(kwargs["allow_redirects"] is False for _, kwargs in client.calls)

    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    monkeypatch.setattr(collection, "collect", lambda *a, **k: pytest.fail("import used network"))
    assert cli.main(["import", "--input", str(output), "--db", str(database)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "imported",
        "source_responses_inserted": 4,
        "observations_inserted": 3,
    }
    with closing(sqlite3.connect(database)) as conn:
        ids = tuple(response.receipt_id for response in capture.responses)
        assert read_responses(conn, ids) == capture.responses
        assert conn.execute(f"SELECT consensus_target FROM {MODEL_VALUATION_TABLE}").fetchone() == (
            None,
        )
    assert _counts(database) == (1, 1, 1)
    assert cli.main(["import", "--input", str(output), "--db", str(database)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "imported",
        "source_responses_inserted": 0,
        "observations_inserted": 0,
    }


def test_failed_and_incomplete_http_200_originals_are_retained_without_selection(
    monkeypatch, tmp_path, capsys
):
    client = BytesSession(
        [
            (200, b"<html>bad ticker identity</html>", {"Content-Type": "text/html"}),
            (200, official_html().encode(), {"Content-Type": "text/html; charset=utf-8"}),
            (500, b"not a captured response", {}),
        ]
    )
    original_collect = collection.collect
    monkeypatch.setattr(source_http, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        collection,
        "collect",
        lambda *args, **kwargs: original_collect(*args, session=client, **kwargs),
    )
    output = tmp_path / "failed.json"
    assert (
        cli.main(
            [
                "collect",
                "--tickers",
                "7203",
                "--source",
                "monex",
                "--source",
                "rakuten",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome_counts"] == {"parse_failure": 1, "http_failure": 1}
    capture = load_capture(output)
    assert len(capture.responses) == 2
    assert capture.selected_attempt_ids == ()
    assert capture.responses[0].body == b"<html>bad ticker identity</html>"
    assert capture.responses[1].body == official_html().encode()
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    assert cli.main(["import", "--input", str(output), "--db", str(database)]) == 0
    capsys.readouterr()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (2,)
    assert _counts(database) == (0, 0, 0)


def test_budget_interruption_retains_only_received_outer_page(tmp_path):
    client = BytesSession(
        [(200, official_html().encode(), {"Content-Type": "text/html; charset=utf-8"})]
    )
    received = []
    result = collection.collect(
        ["7203"],
        sources=("rakuten",),
        request_budget=1,
        session=client,
        on_received_html=received.append,
    )
    assert result.stop_reason == "request_budget"
    assert result.outcomes[0].status == "incomplete"
    assert len(received) == 1
    assert received[0].attempt_id == result.outcomes[0].attempt_id
    assert received[0].role == "page"
    path = tmp_path / "outer-only.json"
    save_capture(path, EquityWebCapture(tuple(received), ()))
    assert load_capture(path).responses == tuple(received)


def test_incomplete_selected_attempt_leaves_l2_empty_but_keeps_originals(tmp_path, capsys):
    valid = _original(body=monex_html(value="--").encode())
    incomplete = EquityWebSourceResponse(
        receipt_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        source="rakuten",
        requested_ticker="7203",
        role="page",
        parent_receipt_id=None,
        requested_url=rakuten.public_url("7203"),
        received_at_utc=valid.received_at_utc,
        selected_encoding="utf-8",
        body=official_html().encode(),
    )
    path = tmp_path / "incomplete.json"
    save_capture(
        path,
        EquityWebCapture((valid, incomplete), (valid.attempt_id, incomplete.attempt_id)),
    )
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    assert cli.main(["import", "--input", str(path), "--db", str(database)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "invalid_input_or_storage"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (2,)
    assert _counts(database) == (0, 0, 0)


def test_incomplete_capture_cannot_borrow_previously_retained_iframe(tmp_path, capsys):
    page, iframe = pair_responses(
        "rakuten", "7203", received_at_utc=datetime(2026, 10, 10, 7, tzinfo=UTC)
    )
    complete = tmp_path / "complete.json"
    save_capture(complete, EquityWebCapture((page, iframe), (page.attempt_id,)))
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    assert cli.main(["import", "--input", str(complete), "--db", str(database)]) == 0
    capsys.readouterr()
    with closing(sqlite3.connect(database)) as conn:
        before = conn.execute(f"SELECT * FROM {ANALYST_RATING_TABLE}").fetchall()
        assert len(before) == 1
        assert read_responses(conn, (page.receipt_id, iframe.receipt_id)) == (page, iframe)

    outer_only = tmp_path / "outer-only.json"
    save_capture(outer_only, EquityWebCapture((page,), (page.attempt_id,)))
    assert cli.main(["import", "--input", str(outer_only), "--db", str(database)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "invalid_input_or_storage"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute(f"SELECT * FROM {ANALYST_RATING_TABLE}").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (2,)


@pytest.mark.parametrize("change", ["bad_base64", "missing_selected", "duplicate_key"])
def test_invalid_capture_rejected_before_import(tmp_path, change, capsys):
    original = _original()
    path = tmp_path / "capture.json"
    save_capture(path, EquityWebCapture((original,), (original.attempt_id,)))
    payload = json.loads(path.read_text())
    if change == "bad_base64":
        payload["responses"][0]["body_base64"] = "@@@"
        path.write_text(json.dumps(payload))
    elif change == "missing_selected":
        payload["selected_attempt_ids"] = [str(uuid.uuid4())]
        path.write_text(json.dumps(payload))
    else:
        path.write_text(path.read_text().replace('"format":', '"format":"duplicate", "format":'))
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    assert cli.main(["import", "--input", str(path), "--db", str(database)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "invalid_input_or_storage"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (0,)


def test_old_normalized_stage_is_explicitly_rejected_and_left_untouched(tmp_path, capsys):
    path = tmp_path / "old.json"
    old = json.dumps({"format": "external-assessment-staging-v1", "observations": []})
    path.write_text(old)
    with pytest.raises(LegacyCaptureError):
        load_capture(path)
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    assert cli.main(["import", "--input", str(path), "--db", str(database)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "legacy_stage_unsupported"
    assert path.read_text() == old
    assert _counts(database) == (0, 0, 0)
