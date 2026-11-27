"""Synthetic US research, independent review and actual file round-trip."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from tools.experiments.us_research_report import main


def _draft():
    return {
        "kind": "us_stock_analytical_report",
        "report_id": "us-report-synthetic-01",
        "author_identity": "synthetic author",
        "venue": "XNAS",
        "ticker": "GOOGL",
        "cik": "0001652044",
        "share_class": "Class A common",
        "currency": "USD",
        "cutoff_at": "2026-11-28T14:00:00Z",
        "prepared_at": "2026-11-28T15:00:00Z",
        "expected_session_et": "2026-11-27",
        "session_close_at": "2026-11-27T13:00:00-05:00",
        "calendar_source_url": "https://www.nasdaq.com/synthetic-calendar",
        "iex_band": {
            "feed": "alpaca_iex",
            "basis": "split_adjusted_closes",
            "manifest_sha256": "1" * 64,
            "received_at": "2026-11-28T06:00:00Z",
            "first_session_et": "2026-11-01",
            "last_session_et": "2026-11-27",
            "observations": 20,
            "low_usd": "110",
            "high_usd": "124",
            "last_usd": "119",
        },
        "official_close": {
            "basis": "regular_session_unadjusted",
            "session_et": "2026-11-27",
            "price_usd": "120",
            "source_url": "https://www.nasdaq.com/synthetic-close",
            "observed_at": "2026-11-27T18:05:00Z",
        },
        "financial_evidence": [
            {
                "source_url": "https://www.sec.gov/synthetic-filing",
                "period_end": "2026-09-30",
                "observed_at": "2026-11-26T14:00:00Z",
                "fact_and_scope": "合成の四半期財務。営業利益率の定義を固定。",
            }
        ],
        "thesis": "合成の収益継続仮説。",
        "strongest_countercase": "合成のコスト増で利益と評価倍率が低下する場合。",
        "comparison": {
            "horizon_et": "2027-11-27",
            "cash_basis": "same_usd_no_interest_tax_or_fees",
            "base": {
                "terminal_value_usd_per_share": "136",
                "eligible_distributions_usd_per_share": "1",
                "assumptions": ["合成の正常利益と倍率。権利取得後の分配だけ。"],
            },
            "downside": {
                "terminal_value_usd_per_share": "90",
                "eligible_distributions_usd_per_share": "1",
                "assumptions": ["合成のマージン低下と倍率縮小。"],
            },
        },
        "unresolved": [],
    }


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _prepare(tmp_path, capsys, payload):
    draft = _write(tmp_path / "draft.json", payload)
    assert main(["draft-digest", draft]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    binding = json.loads(output.out)
    review = {
        **binding,
        "reviewer_identity": "synthetic-reviewer",
        "reviewed_at": "2026-11-28T16:00:00Z",
        "conclusion": "pass",
        "notes": "価格・不確実性・現金の各算術を別に確認。",
    }
    return draft, review


@pytest.mark.parametrize("has_close", [True, False])
@pytest.mark.parametrize("ticker", ["GOOGL", "GOOG", "BRK.B", "AB12.CD3"])
def test_reviewed_report_round_trips_without_substitution_or_overwrite(
    tmp_path, capsys, has_close, ticker
):
    payload = _draft()
    payload["ticker"] = ticker
    if ticker == "GOOG":
        payload["share_class"] = "Class C common"
    elif ticker == "BRK.B":
        payload.update(
            venue="XNYS",
            cik="0000000001",
            share_class="Class B common",
            calendar_source_url="https://www.nyse.com/synthetic-calendar",
        )
        payload["official_close"]["source_url"] = "https://www.nyse.com/synthetic-close"
    if not has_close:
        payload.update(official_close=None, comparison=None, unresolved=["取引所終値未確認。"])
    draft, review = _prepare(tmp_path, capsys, payload)
    review_path = _write(tmp_path / "review.json", review)
    saved = tmp_path / "reviewed.json"
    assert main(["save", draft, review_path, "--output", str(saved)]) == 0
    before = saved.read_bytes()
    assert main(["render", str(saved)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    shown = captured.out
    assert review["report_id"] in shown
    assert review["draft_sha256"] in shown
    assert shown.startswith(f"# {ticker} ")
    assert json.loads(before)["draft"]["ticker"] == ticker
    assert "正式提案未発行" in shown
    assert "USD 110 ～ 124 / 株、末尾 USD 119 / 株" in shown
    assert "2026-11-27T13:00:00-05:00" in shown
    if has_close:
        assert "上場取引所の無調整終値: USD 120" in shown
        assert "現金との差 USD +17" in shown
        assert "現金との差 USD -29" in shown
    else:
        assert "IEXで代用していません" in shown
        assert "判断保留" in shown
        assert "現金との差 USD" not in shown
    rendered = tmp_path / "report.md"
    rendered.write_bytes(shown.encode())
    assert json.loads(before)["rendered_sha256"] == sha256(rendered.read_bytes()).hexdigest()
    assert main(["check-render", str(saved), str(rendered)]) == 0
    assert capsys.readouterr().out == "PASS exact private analytical report render\n"
    assert main(["save", draft, review_path, "--output", str(saved)]) == 2
    assert saved.read_bytes() == before
    rendered.write_bytes(rendered.read_bytes() + b"\nDifferent prose, same header\n")
    assert main(["check-render", str(saved), str(rendered)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("FAIL rendered file\n")
    assert saved.read_bytes() == before


@pytest.mark.parametrize(
    ("fields", "value"),
    [
        (("currency",), "JPY"),
        (("ticker",), "A."),
        (("ticker",), "A..B"),
        (("ticker",), "A.B.C"),
        (("ticker",), ".ABC"),
        (("ticker",), "BRK-B"),
        (("ticker",), "BRK/B"),
        (("ticker",), "BRK.B "),
        (("ticker",), "brk.b"),
        (("ticker",), "ＡAPL"),
        (("ticker",), "ABC12.CD3"),
        (("buy_quantity",), 100),
        (("official_close", "basis"), "split_adjusted_closes"),
        (("official_close", "price_usd"), 0),
        (("official_close", "source_url"), "https://nasdaq.com.example/private-token"),
        (("official_close", "source_url"), "https://data.alpaca.markets/private-token"),
        (("official_close", "observed_at"), "2026-11-27T17:59:59Z"),
        (("official_close", "session_et"), "2026-11-26"),
        (("official_close",), None),
        (("iex_band", "received_at"), "2026-11-27T23:50:00Z"),
        (("iex_band", "last_session_et"), "2026-11-25"),
        (("iex_band", "last_usd"), "125"),
        (("session_close_at",), "2026-11-28T16:00:00-05:00"),
        (("calendar_source_url",), "https://data.alpaca.markets/private-token"),
        (("prepared_at",), "2026-11-28T13:59:59Z"),
        (("comparison", "horizon_et"), "2026-11-28"),
        (("comparison", "downside", "terminal_value_usd_per_share"), "150"),
        (("comparison", "base", "assumptions"), []),
        (("comparison",), None),
    ],
)
def test_invalid_draft_never_produces_a_review_binding(tmp_path, capsys, fields, value):
    draft = _draft()
    owner = draft
    for key in fields[:-1]:
        owner = owner[key]
    owner[fields[-1]] = value
    assert main(["draft-digest", _write(tmp_path / "invalid.json", draft)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAIL draft\n"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_id", "us-report-another-issuer"),
        ("draft_sha256", "0" * 64),
        ("reviewer_identity", "  SYNTHETIC AUTHOR  "),
        ("reviewer_identity", "  SYNTHETIC\t  AUTHOR  "),
        ("reviewed_at", "2026-11-28T14:59:59Z"),
        ("conclusion", "changes_requested"),
    ],
)
def test_unbound_or_nonindependent_review_cannot_be_saved(tmp_path, capsys, field, value):
    draft, review = _prepare(tmp_path, capsys, _draft())
    review[field] = value
    out = tmp_path / "not-created.json"
    assert (
        main(["save", draft, _write(tmp_path / "review.json", review), "--output", str(out)]) == 2
    )
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAIL review/save\n"


def test_changed_saved_draft_with_same_id_and_a_duplicate_json_key_are_rejected(tmp_path, capsys):
    draft, review = _prepare(tmp_path, capsys, _draft())
    saved = tmp_path / "reviewed.json"
    assert (
        main(["save", draft, _write(tmp_path / "review.json", review), "--output", str(saved)]) == 0
    )
    altered = json.loads(saved.read_bytes())
    altered["draft"]["thesis"] = "This is not the reviewed draft."
    a = _write(tmp_path / "tampered.json", altered)
    assert main(["render", a]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAIL saved report\n"

    raw = Path(draft).read_text(encoding="utf-8")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"currency": "JPY", ' + raw[1:], encoding="utf-8")
    assert main(["draft-digest", str(duplicate)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "FAIL draft\n"
    assert captured.out == ""
