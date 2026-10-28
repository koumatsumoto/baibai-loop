"""Offline L1 replay uses only complete selected originals and replaces exact L2 keys."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.engine.test_external_assessment_sources import (
    monex_html,
    official_html,
    rakuten_html,
)
from tests.engine.test_kabumap import html as kabumap_html

from baibai_engine.data_collection.equity_web.receipts import read_responses, retain_response
from baibai_engine.cli.external_assessment import main as assessment_main
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
    kabumap,
    monex,
    rakuten,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import decode_attempt
from baibai_engine.machine_analysis.observations.external_assessments.identity import (
    MODEL_VALUATION_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.rebuild import rebuild_attempts
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

RECEIVED = datetime(2026, 10, 10, 16, 30, 12, 345678, tzinfo=UTC)


def _capture(
    conn,
    *,
    source="monex",
    ticker="7203",
    attempt=None,
    role="page",
    parent=None,
    body=None,
    encoding="shift_jis",
    at=RECEIVED,
    url=None,
):
    attempt = str(uuid.uuid4()) if attempt is None else attempt
    if url is None:
        url = (
            rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + ticker + ".T"
            if role == "iframe"
            else {"monex": monex, "rakuten": rakuten, "kabumap": kabumap}[source].public_url(ticker)
        )
    if body is None:
        text = (
            rakuten_html(ticker)
            if role == "iframe"
            else monex_html(ticker)
            if source == "monex"
            else official_html(ticker)
            if source == "rakuten"
            else kabumap_html(ticker)
        )
        body = text.encode(encoding)
    receipt_id = retain_response(
        conn,
        attempt_id=attempt,
        source=source,
        requested_ticker=ticker,
        role=role,
        parent_receipt_id=parent,
        requested_url=url,
        received_at_utc=at,
        selected_encoding=encoding,
        body=body,
    )
    return attempt, receipt_id


def _responses(conn, attempt):
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT receipt_id FROM equity_web_source_responses WHERE attempt_id=? ORDER BY receipt_id",
            (attempt,),
        )
    ]
    return read_responses(conn, ids)


def _pair(conn, *, ticker="7203", at=RECEIVED):
    attempt = str(uuid.uuid4())
    _, page = _capture(conn, source="rakuten", ticker=ticker, attempt=attempt, at=at)
    _capture(
        conn,
        source="rakuten",
        ticker=ticker,
        attempt=attempt,
        role="iframe",
        parent=page,
        at=at + timedelta(seconds=2),
    )
    return attempt


@pytest.mark.parametrize(
    ("source", "expected_version", "time_shift"),
    [
        ("monex", "monex-theory-3", 0),
        ("kabumap", "kabumap-quants-3", 0),
        ("rakuten", "rakuten-rating-3", 2),
    ],
)
def test_each_captured_source_decodes_its_original_bytes_encoding_and_time(
    tmp_path, source, expected_version, time_shift
):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        ticker = "130A" if source == "kabumap" else "7203"
        attempt = (
            _pair(conn) if source == "rakuten" else _capture(conn, source=source, ticker=ticker)[0]
        )
        responses = _responses(conn, attempt)
        assert all(response.selected_encoding == "shift_jis" for response in responses)
        for response in responses:
            expected_html = (
                rakuten_html(ticker)
                if response.role == "iframe"
                else monex_html(ticker)
                if source == "monex"
                else official_html(ticker)
                if source == "rakuten"
                else kabumap_html(ticker)
            )
            assert response.body == expected_html.encode("shift_jis")
            assert response.received_at_utc == RECEIVED + timedelta(
                seconds=2 if response.role == "iframe" else 0
            )
        observation = decode_attempt(responses)
        assert observation.ticker == ticker
        assert observation.parser_version == expected_version
        assert observation.fetched_at_utc == RECEIVED + timedelta(seconds=time_shift)


@pytest.mark.parametrize("source", ["monex", "kabumap", "rakuten"])
def test_verified_normal_null_remains_a_typed_observation(tmp_path, source):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        if source == "rakuten":
            attempt = str(uuid.uuid4())
            _, page = _capture(conn, source="rakuten", attempt=attempt)
            _capture(
                conn,
                source="rakuten",
                attempt=attempt,
                role="iframe",
                parent=page,
                body=rakuten_html(value="--").encode("shift_jis"),
            )
        else:
            text = monex_html(value="--") if source == "monex" else kabumap_html(cheap="NA/10")
            attempt = _capture(
                conn,
                source=source,
                ticker="130A" if source == "kabumap" else "7203",
                body=text.encode("shift_jis"),
            )[0]
        observation = decode_attempt(_responses(conn, attempt))
        value = (
            observation.consensus_target
            if source == "monex"
            else observation.undervaluation_score
            if source == "kabumap"
            else observation.rating
        )
        assert value is None


def test_rakuten_rejects_wrong_iframe_url_parent_and_cross_attempt(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        attempt = _pair(conn)
        page, iframe = sorted(
            _responses(conn, attempt), key=lambda response: response.role == "iframe"
        )
        with pytest.raises(ParseError, match="source_identity"):
            decode_attempt((page, replace(iframe, requested_url=iframe.requested_url[:-2])))
        with pytest.raises(ParseError, match="parent_identity"):
            decode_attempt((page, replace(iframe, parent_receipt_id=str(uuid.uuid4()))))
        with pytest.raises(ParseError, match="attempt_identity"):
            decode_attempt((page, replace(iframe, attempt_id=str(uuid.uuid4()))))
        with pytest.raises(ParseError, match="incomplete_attempt"):
            decode_attempt((page,))
        with pytest.raises(ParseError, match="duplicate_receipt"):
            decode_attempt((page, page))
        with pytest.raises(ParseError, match="duplicate_response_role"):
            decode_attempt((page, replace(page, receipt_id=str(uuid.uuid4()))))


def test_strict_saved_encoding_and_malformed_html_fail_instead_of_normal_null(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        attempt = _capture(conn)[0]
        response = _responses(conn, attempt)[0]
        with pytest.raises(ParseError, match="response_encoding"):
            decode_attempt((replace(response, selected_encoding="utf-8"),))
        with pytest.raises(ParseError, match="ticker_identity"):
            decode_attempt((replace(response, body=b"<html>no stock identity</html>"),))


def test_selected_replacement_matches_clean_parse_and_preserves_other_keys_and_l1(
    tmp_path, monkeypatch
):
    import requests

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *_args, **_kwargs: pytest.fail("offline rebuild attempted a network call"),
    )
    target = tmp_path / "target.sqlite"
    clean = tmp_path / "clean.sqlite"
    with closing(open_connection(target)) as conn:
        attempt = _capture(conn)[0]
        other_attempt = _capture(conn, ticker="9432")[0]
        parsed = decode_attempt(_responses(conn, attempt))
        other = decode_attempt(_responses(conn, other_attempt))
        original_l1 = conn.execute(
            "SELECT receipt_id, body_gzip FROM equity_web_source_responses ORDER BY receipt_id"
        ).fetchall()
    with closing(open_connection(clean)) as conn:
        clean_attempt = _capture(conn)[0]
    stale = parsed.model_copy(update={"consensus_target": 1.0})
    save_observations(target, [stale, other])
    assert rebuild_attempts(target, attempt_ids=[attempt]) == 1
    assert rebuild_attempts(clean, attempt_ids=[clean_attempt]) == 1
    with closing(sqlite3.connect(target)) as conn, closing(sqlite3.connect(clean)) as clean_conn:
        rebuilt = conn.execute(
            f"SELECT * FROM {MODEL_VALUATION_TABLE} WHERE ticker='7203'"
        ).fetchone()
        expected = clean_conn.execute(
            f"SELECT * FROM {MODEL_VALUATION_TABLE} WHERE ticker='7203'"
        ).fetchone()
        assert rebuilt == expected
        assert (
            rebuilt
            != conn.execute(f"SELECT * FROM {MODEL_VALUATION_TABLE} WHERE ticker='9432'").fetchone()
        )
        assert conn.execute(
            f"SELECT COUNT(*) FROM {MODEL_VALUATION_TABLE} WHERE ticker='9432'"
        ).fetchone() == (1,)
        assert (
            conn.execute(
                "SELECT receipt_id, body_gzip FROM equity_web_source_responses ORDER BY receipt_id"
            ).fetchall()
            == original_l1
        )


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("incomplete", "incomplete_attempt"),
        ("malformed", "ticker_identity"),
        ("missing", "not retained"),
        ("duplicate_output", "duplicate output key"),
    ],
)
def test_invalid_selected_attempt_leaves_l2_untouched(tmp_path, invalid, message):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn:
        valid = _capture(conn)[0]
        parsed = decode_attempt(_responses(conn, valid))
        if invalid == "incomplete":
            bad = _capture(conn, source="rakuten")[0]
        elif invalid == "malformed":
            bad = _capture(conn, ticker="9432", body=b"<html>invalid</html>")[0]
        elif invalid == "missing":
            bad = str(uuid.uuid4())
        else:
            bad = _capture(conn)[0]
    stale = parsed.model_copy(update={"consensus_target": 1.0})
    save_observations(path, [stale])
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute(f"SELECT * FROM {MODEL_VALUATION_TABLE}").fetchall()
    with pytest.raises(ValueError, match=message):
        rebuild_attempts(path, attempt_ids=[valid, bad])
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(f"SELECT * FROM {MODEL_VALUATION_TABLE}").fetchall() == before


def test_sql_failure_rolls_back_all_selected_keys(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn:
        first = _capture(conn, ticker="7203")[0]
        second = _capture(conn, ticker="9432")[0]
        first_parsed = decode_attempt(_responses(conn, first))
        second_parsed = decode_attempt(_responses(conn, second))
    save_observations(
        path,
        [
            first_parsed.model_copy(update={"consensus_target": 1.0}),
            second_parsed.model_copy(update={"consensus_target": 2.0}),
        ],
    )
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute(f"SELECT * FROM {MODEL_VALUATION_TABLE} ORDER BY ticker").fetchall()
        raw_before = conn.execute(
            "SELECT receipt_id, body_gzip FROM equity_web_source_responses ORDER BY receipt_id"
        ).fetchall()
        conn.execute(
            f"CREATE TRIGGER fail_second_insert BEFORE INSERT ON {MODEL_VALUATION_TABLE} "
            "WHEN NEW.ticker='9432' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        rebuild_attempts(path, attempt_ids=[first, second])
    with closing(sqlite3.connect(path)) as conn:
        assert (
            conn.execute(f"SELECT * FROM {MODEL_VALUATION_TABLE} ORDER BY ticker").fetchall()
            == before
        )
        assert (
            conn.execute(
                "SELECT receipt_id, body_gzip FROM equity_web_source_responses ORDER BY receipt_id"
            ).fetchall()
            == raw_before
        )


def test_missing_db_and_old_schema_are_never_bootstrapped(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        rebuild_attempts(missing, attempt_ids=[str(uuid.uuid4())])
    assert not missing.exists()
    sqlite3.connect(missing).close()
    with pytest.raises(SQLiteSchemaError):
        rebuild_attempts(missing, attempt_ids=[str(uuid.uuid4())])


def test_cli_requires_unique_explicit_attempts_and_rebuilds_multiple(tmp_path, capsys):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn:
        first = _capture(conn, ticker="7203")[0]
        second = _capture(conn, source="kabumap", ticker="130A")[0]
    args = ["rebuild-raw", "--db", str(path), "--attempt-id", first, "--attempt-id", second]
    assert assessment_main(args) == 0
    assert '"observations_rebuilt": 2' in capsys.readouterr().out
    assert assessment_main([*args, "--attempt-id", first]) == 1
    assert "rebuild failed" in capsys.readouterr().err
