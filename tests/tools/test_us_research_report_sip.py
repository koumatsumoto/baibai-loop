"""Synthetic receipt proof for new SIP writes; ordinary persisted reads need no raw file."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.tools.test_us_research_report import _draft
from tools.experiments.us_research_report import main

from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
    read_nasdaq_official_close,
)
from baibai_engine.investment_judgment.us_proposal import ReportDraft, sip_official_close_fields
from baibai_engine.storage.sqlite.application.write import initialize_database

DAY = date(2025, 6, 2)
CLOSE = datetime(2025, 6, 2, 20, tzinfo=UTC)
RECEIVED = CLOSE + timedelta(hours=2)
QUERY = NasdaqOfficialCloseQuery("GOOGL", CLOSE - timedelta(minutes=5), CLOSE + timedelta(hours=1))
BODY = b"""{
"symbol":"GOOGL","next_page_token":null,"auctions":[{"d":"2025-06-02","o":null,"c":[
{"x":"Q","c":"6","p":117.31,"t":"2025-06-02T20:00:00.200000001Z"},
{"x":"P","c":"M","p":119.12,"t":"2025-06-02T20:00:00.200000002Z"},
{"x":"Q","c":"M","p":120.0100,"t":"2025-06-02T20:00:00.200000003Z"}]}]}
"""


def _json(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.fixture
def inputs(tmp_path):
    metadata = {
        "request_url": QUERY.url(),
        "response_url": QUERY.url(),
        "status": 200,
        "received_at": RECEIVED.isoformat(),
        "sha256": sha256(BODY).hexdigest(),
        "complete": True,
        "redirected": False,
    }
    proof = SipAuctionReceipt(
        QUERY.url(), QUERY.url(), 200, RECEIVED, sha256(BODY).hexdigest(), True, False, BODY
    )
    cut = RECEIVED + timedelta(minutes=15)
    observed = read_nasdaq_official_close(
        QUERY,
        proof,
        listing_venue="XNAS",
        session_et=DAY,
        session_close_at=CLOSE,
        cutoff_at=cut,
    )
    draft = _draft()
    draft.update(
        ticker="GOOGL",
        official_close=sip_official_close_fields(observed),
        cutoff_at=cut.isoformat(),
        prepared_at="2025-06-03T01:00:00Z",
        expected_session_et=str(DAY),
        session_close_at=CLOSE.isoformat(),
        iex_band=None,
        unresolved=["合成IEX観測なし。別フィードに置換しない。"],
    )
    draft["financial_evidence"][0].update(
        period_end="2025-03-31",
        observed_at="2025-05-19T12:00:00Z",
    )
    draft["comparison"]["horizon_et"] = (datetime.now(UTC) + timedelta(days=400)).date().isoformat()
    metadata_path = tmp_path / "synthetic-capture.json"
    original_path = tmp_path / "synthetic-native.json"
    _json(metadata_path, metadata)
    original_path.write_bytes(BODY)
    return draft, metadata_path, original_path


def test_relayed_report_and_proposal_bind_receipt_before_new_writes(inputs, tmp_path, capsys):
    draft, metadata_path, original_path = inputs
    authored = _json(tmp_path / "draft.json", draft)
    proof = ["--sip-proof", str(metadata_path), str(original_path)]
    assert main(["draft-digest", authored]) == 2
    assert capsys.readouterr().err == "FAIL draft\n"
    assert main(["draft-digest", authored, *proof]) == 0
    analysis_review = _json(
        tmp_path / "analysis-review.json",
        {
            **json.loads(capsys.readouterr().out),
            "reviewer_identity": "synthetic independent analyst",
            "reviewed_at": "2025-06-03T02:00:00Z",
            "conclusion": "pass",
            "notes": "別担当の合成原本テスト。",
        },
    )
    saved = tmp_path / "analysis.json"
    save = ["save", authored, analysis_review, "--output", str(saved)]
    assert main(save) == 2
    assert not saved.exists()
    capsys.readouterr()
    assert main([*save, *proof]) == 0
    assert main(["render", str(saved)]) == 0
    rendered = capsys.readouterr().out
    assert "Nasdaqの無調整公式終値（Alpaca SIP経由）: USD 120.0100" in rendered
    assert "2025-06-02T20:00:00.200000003Z" in rendered
    assert "該当箇所: /auctions/0/c/2" in rendered
    assert "現金との差 USD +16.9900" in rendered
    assert json.loads(saved.read_bytes())["draft"]["official_close"] == draft["official_close"]

    decision = _json(
        tmp_path / "decision.json",
        {
            "proposal_id": "us-proposal-synthetic-sip",
            "author_identity": "synthetic decision author",
            "prepared_at": "2025-06-03T03:00:00Z",
            "action": "no_action",
            "reason": "合成価格が現金を上回る条件を検討。",
            "revisit_when": "合成の前提が変わったとき。",
        },
    )
    digest = ["proposal-digest", str(saved), decision]
    assert main(digest) == 2
    capsys.readouterr()
    assert main([*digest, *proof]) == 0
    binding = json.loads(capsys.readouterr().out)
    second_review = _json(
        tmp_path / "decision-review.json",
        {
            **binding,
            "reviewer_identity": "synthetic independent decision reviewer",
            "reviewed_at": "2025-06-03T04:00:00Z",
            "conclusion": "pass",
            "notes": "合成計算の再確認。",
        },
    )
    app = tmp_path / "isolated.sqlite"
    initialize_database(app)
    before = app.read_bytes()
    publish = ["proposal-save", str(saved), decision, second_review, "--db", str(app)]
    assert main(publish) == 2
    assert app.read_bytes() == before
    assert capsys.readouterr().err == "FAIL US proposal save\n"
    assert main([*publish, *proof]) == 0
    assert json.loads(capsys.readouterr().out) == binding
    # Historical reads must not rely on the privileged original capture files.
    metadata_path.unlink()
    original_path.unlink()
    before = app.read_bytes()
    assert main(["proposal-show", "--db", str(app), "--proposal-id", binding["proposal_id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["analysis"] == json.loads(saved.read_bytes())
    assert shown["review"]["draft_sha256"] == binding["draft_sha256"]
    assert main(["render", str(saved)]) == 0
    assert capsys.readouterr().out == rendered
    with closing(sqlite3.connect(app)) as connection:
        assert connection.execute("SELECT count(*) FROM ledger_event").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM investment_proposal").fetchone() == (0,)
    assert app.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "listing_exchange"),
        ("venue", "XNYS"),
        ("symbol", "GOOG"),
        ("source_url", "https://www.nasdaq.com/synthetic-close"),
        ("native_response_sha256", "1" * 64),
        ("source_json_pointer", "/auctions/0/c/1"),
        ("event_at_rfc3339", "2025-06-02T20:00:00.200000001Z"),
        ("price_usd", "120.01"),  # Same numeric value, not the captured representation.
        ("sale_condition", "6"),
        ("exchange_code", "P"),
    ],
)
def test_sip_label_fields_cannot_rewrite_actual_quote(inputs, tmp_path, capsys, field, value):
    draft, metadata, raw = inputs
    draft["official_close"][field] = value
    path = _json(tmp_path / "changed.json", draft)
    assert main(["draft-digest", path, "--sip-proof", str(metadata), str(raw)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAIL draft\n"


@pytest.mark.parametrize(
    "problem", ["missing-complete", "unknown-redirect", "raw-lie", "extra-time"]
)
def test_incomplete_or_replaced_transfer_is_not_a_closed_quote(inputs, tmp_path, capsys, problem):
    draft, metadata, raw = inputs
    info = json.loads(metadata.read_bytes())
    if problem == "missing-complete":
        info.pop("complete")
    elif problem == "unknown-redirect":
        info.pop("redirected")
    elif problem == "raw-lie":
        raw.write_bytes(BODY + b" ")
    else:
        info["received_at"] = "2025-06-02T22:00:00.0000001Z"
    _json(metadata, info)
    path = _json(tmp_path / "draft.json", draft)
    assert main(["draft-digest", path, "--sip-proof", str(metadata), str(raw)]) == 2
    assert capsys.readouterr().out == ""


def test_alpaca_uri_is_not_a_direct_exchange_close(inputs, tmp_path, capsys):
    draft, metadata, raw = inputs
    direct = _draft()["official_close"]
    for key in direct:
        direct[key] = draft["official_close"][key]
    draft["official_close"] = direct
    path = _json(tmp_path / "incorrect-direct.json", draft)
    for args in ([], ["--sip-proof", str(metadata), str(raw)]):
        assert main(["draft-digest", path, *args]) == 2
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("clock", "accepted"),
    [
        ("2025-06-02T22:00:00Z", True),
        ("2025-06-02T23:00:00+01:00", True),
        ("2025-06-02T23:00:00+00:60", False),
        ("2025-06-02T21:00:00-00:60", False),
        ("2025-06-02T22:00:00+24:00", False),
        ("2025-06-02T22:00:00-00:00", False),
    ],
)
def test_sip_receive_offset_must_not_be_normalized_out_of_range(
    inputs, tmp_path, capsys, clock, accepted
):
    draft, metadata, raw = inputs
    path = _json(tmp_path / "draft.json", draft)
    proof = ["draft-digest", path, "--sip-proof", str(metadata), str(raw)]
    assert main(proof) == 0
    reference = capsys.readouterr()
    info = json.loads(metadata.read_bytes())
    info["received_at"] = clock
    _json(metadata, info)
    assert main(proof) == (0 if accepted else 2)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        (reference.out, "") if accepted else ("", "FAIL draft\n")
    )


def test_direct_listing_origin_is_not_a_new_authored_field():
    payload = _draft()
    draft = ReportDraft.model_validate(payload)
    assert draft.official_close is not None
    assert draft.official_close.origin == "listing_exchange"
    assert draft.official_close.model_dump(mode="json") == payload["official_close"]
    payload["official_close"]["origin"] = "listing_exchange"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReportDraft.model_validate(payload)
