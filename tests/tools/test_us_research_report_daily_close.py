"""The non-executable formal proposal can use a retained SIP bar without IEX or official close."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from tests.helpers.us_research_admission import run_with_synthetic_current as main
from tests.tools.test_us_research_report import _draft, _write

from baibai_engine.data_collection.providers.alpaca import DailyBarsQuery
from baibai_engine.storage.paths import APPLICATION_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.materialize import export_read_models

DAY = "2025-11-28"  # 13:00 ET early close in the synthetic report's market calendar.
PREVIOUS = "2025-11-26"
RECEIVED = "2025-11-28T18:34:00Z"  # Same ET date, not an overnight IEX receipt.


def _input(tmp_path, ticker, *, fault=None):
    raw = _draft(ticker=ticker)
    observed_symbol = "GOOG" if fault == "wrong-class" else ticker
    observation_day = PREVIOUS if fault == "wrong-day" else DAY
    observed_at = (
        "2025-11-28T17:59:59Z"
        if fault == "before-close"
        else "2025-11-29T14:00:00.000001Z"
        if fault == "after-cutoff"
        else RECEIVED
    )
    query = DailyBarsQuery(
        tuple(sorted({"GOOG", ticker})),
        datetime.fromisoformat(PREVIOUS).date(),
        datetime.fromisoformat(DAY).date(),
        "-" if fault != "alias" else datetime.fromisoformat(DAY).date(),
        "split" if fault == "split" else "raw",
        feed="iex" if fault == "iex" else "sip",
    )
    cursor = '"next"' if fault == "missing-page" else "null"
    body = (
        f'{{"bars":{{"{observed_symbol}":[{{"t":"{observation_day}T05:00:00Z",'
        '"o":115,"h":127,"l":110,"c":120.0100,"v":120002}]},"next_page_token":'
        f"{cursor}" + "}"
    ).encode()
    if fault == "zero-volume":
        body = body.replace(b'"v":120002', b'"v":0')
    body_path = tmp_path / "bars.json"
    body_path.write_bytes(body)
    url = (
        "https://data.alpaca.markets/v2/stocks/GOOGL/trades/latest?feed=sip"
        if fault == "latest"
        else query.url()
    )
    digest = sha256(body).hexdigest()
    manifest = _write(
        tmp_path / "capture.json",
        {
            "request_id": "synthetic-sip-daily",
            "start": PREVIOUS,
            "end": DAY,
            "symbols": list(query.symbols),
            "pages": [
                {
                    "file": body_path.name,
                    "sha256": "1" * 64 if fault == "sha" else digest,
                    "status": 200,
                    "received_at": observed_at,
                    "request_url": url,
                    "response_url": url,
                    "complete": fault != "incomplete",
                    "redirected": False,
                }
            ],
        },
    )
    raw["financial_evidence"][0].update(period_end="2025-09-30", observed_at="2025-11-26T20:30:00Z")
    official = raw.pop("official_close")
    raw.pop("iex_band")
    if fault == "two-prices":
        official.update(session_et=DAY, observed_at=RECEIVED)
        raw["official_close"] = official
    raw.update(
        cutoff_at="2025-11-29T14:00:00Z",
        prepared_at="2025-11-29T15:00:00Z",
        expected_session_et=DAY,
        session_close_at="2025-11-28T13:00:00-05:00",
        daily_close={
            "origin": "alpaca_sip_daily_close",
            "basis": "vendor_daily_aggregate_unadjusted",
            "symbol": ticker,
            "session_et": DAY,
            "price_usd": "120.0200" if fault == "price" else "120.0100",
            "observed_at": RECEIVED,
            "native_response_sha256": digest,
            "source_json_pointer": f"/bars/{ticker}/0",
        },
    )
    raw["comparison"]["horizon_et"] = (datetime.now(UTC) + timedelta(days=450)).date().isoformat()
    return raw, manifest, body_path


@pytest.mark.parametrize("ticker", ["GOOGL", "BRK.B"])
def test_daily_close_binds_the_full_authoring_and_web_readback(ticker, tmp_path, capsys):
    raw, manifest, body_path = _input(tmp_path, ticker)
    proof = ["--daily-close-proof", manifest]
    source = _write(tmp_path / "draft.json", raw)
    assert main(["draft-digest", source]) == 2
    assert capsys.readouterr().err == "FAIL draft\n"
    assert main(["draft-digest", source, *proof]) == 0
    analysis_review = _write(
        tmp_path / "analysis-review.json",
        {
            **json.loads(capsys.readouterr().out),
            "reviewer_identity": "synthetic analyst peer",
            "reviewed_at": "2025-11-29T16:00:00Z",
            "conclusion": "pass",
            "notes": "合成のSIP日足を独立に確認。",
        },
    )
    saved = tmp_path / "reviewed-analysis.json"
    save = ["save", source, analysis_review, "--output", str(saved)]
    assert main(save) == 2
    assert not saved.exists()
    capsys.readouterr()
    assert main([*save, *proof]) == 0
    assert main(["render", str(saved)]) == 0
    rendered = capsys.readouterr().out
    assert "Alpaca SIPの無調整日足終値: USD 120.0100" in rendered
    assert "取引所の公式終値・約定値ではありません。" in rendered
    assert "現金との差 USD +16.9900" in rendered
    assert "IEX観測帯: 未確認。" not in rendered
    decision = _write(
        tmp_path / "decision.json",
        {
            "proposal_id": "us-proposal-synthetic-sip-bars",
            "author_identity": "synthetic decision author",
            "prepared_at": "2025-11-29T17:00:00Z",
            "action": "no_action",
            "reason": "合成の比較で現金を上回る確度が低い。",
            "revisit_when": "合成のシナリオが変わったとき。",
        },
    )
    bind = ["proposal-digest", str(saved), decision]
    assert main(bind) == 2
    capsys.readouterr()
    assert main([*bind, *proof]) == 0
    binding = json.loads(capsys.readouterr().out)
    judgment_review = _write(
        tmp_path / "judgment-review.json",
        {
            **binding,
            "reviewer_identity": "synthetic independent judgment reviewer",
            "reviewed_at": "2025-11-29T18:00:00Z",
            "conclusion": "pass",
            "notes": "基準価格と現金の比較を独立に確認。",
        },
    )
    db = tmp_path / APPLICATION_DB_PATH
    initialize_database(db)
    before = db.read_bytes()
    save_decision = ["proposal-save", str(saved), decision, judgment_review, "--db", str(db)]
    assert main(save_decision) == 2
    assert db.read_bytes() == before
    capsys.readouterr()
    assert main([*save_decision, *proof]) == 0
    assert json.loads(capsys.readouterr().out) == binding
    # A finished judgment is self-contained; its read path needs no provider proof files.
    body_path.unlink()
    assert main(["proposal-show", "--db", str(db), "--proposal-id", binding["proposal_id"]]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["analysis"]["draft"]["daily_close"] == raw["daily_close"]
    assert record["analysis"]["draft"]["official_close"] is None
    assert record["review"]["draft_sha256"] == binding["draft_sha256"]
    assert main(["render", str(saved)]) == 0
    assert capsys.readouterr().out == rendered
    export_read_models(tmp_path, tmp_path / "serving")
    view = json.loads((tmp_path / "serving/views/latest-us-proposal.json").read_bytes())["proposal"]
    assert view["comparison_price"] == {
        "origin": "alpaca_sip_daily_close",
        "price_usd": "120.0100",
        "observed_at": RECEIVED,
    }
    assert view["comparison"]["base"]["difference_from_cash_usd_per_share"] == "16.9900"
    assert view["draft_sha256"] == binding["draft_sha256"]
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM ledger_event").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM investment_proposal").fetchone() == (0,)


@pytest.mark.parametrize(
    "fault",
    [
        "iex",
        "split",
        "alias",
        "wrong-class",
        "wrong-day",
        "before-close",
        "after-cutoff",
        "latest",
        "missing-page",
        "incomplete",
        "sha",
        "price",
        "zero-volume",
        "two-prices",
    ],
)
def test_nonmatching_capture_cannot_prepare_a_daily_price(tmp_path, capsys, fault):
    raw, manifest, _ = _input(tmp_path, "GOOGL", fault=fault)
    source = _write(tmp_path / "draft.json", raw)
    assert main(["draft-digest", source, "--daily-close-proof", manifest]) == 2
    capture = capsys.readouterr()
    assert (capture.out, capture.err) == ("", "FAIL draft\n")
