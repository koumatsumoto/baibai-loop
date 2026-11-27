"""Synthetic US research, independent review and actual file round-trip."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from tools.experiments.us_research_report import main

from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReviewedReport,
    USProposalDraft,
    USProposalReview,
    prepare_us_proposal,
    save_us_proposal,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.paths import APPLICATION_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.materialize import export_read_models


def _draft(*, ticker="GOOGL"):
    draft = {
        "kind": "us_stock_analytical_report",
        "report_id": "us-report-synthetic-01",
        "author_identity": "synthetic author",
        "venue": "XNAS",
        "ticker": ticker,
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
    if ticker == "AAPL":
        draft.update(cik="0000320193", share_class="common")
    elif ticker == "GOOG":
        draft["share_class"] = "Class C common"
    elif ticker == "BRK.B":
        draft.update(
            venue="XNYS",
            cik="0001067983",
            share_class="Class B common",
            calendar_source_url="https://www.nyse.com/synthetic-calendar",
        )
        draft["official_close"]["source_url"] = "https://www.nyse.com/synthetic-close"
    return draft


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
@pytest.mark.parametrize("ticker", ["AAPL", "GOOG", "GOOGL", "BRK.B", "AB12.CD3"])
def test_reviewed_report_round_trips_without_substitution_or_overwrite(
    tmp_path, capsys, has_close, ticker
):
    payload = _draft(ticker=ticker)
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
    "ticker",
    ["A.", "A..B", "A.B.C", ".ABC", "BRK-B", "BRK/B", "BRK.B ", "brk.b", "ＡAPL", "ABC12.CD3"],
)
def test_invalid_ticker_is_rejected_by_owner_and_cli(tmp_path, capsys, ticker):
    payload = _draft(ticker=ticker)
    with pytest.raises(ValidationError) as caught:
        ReportDraft.model_validate(payload)
    assert [error["loc"] for error in caught.value.errors()] == [("ticker",)]

    assert main(["draft-digest", _write(tmp_path / "invalid.json", payload)]) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "FAIL draft\n")


@pytest.mark.parametrize(
    ("fields", "value"),
    [
        (("currency",), "JPY"),
        (("cik",), "0000000000"),
        (("cik",), "1652044"),
        (("cik",), "00000000001"),
        (("cik",), "000000０００１"),
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


def _decision_inputs(tmp_path, capsys, *, comparable, horizon=None, ticker="GOOGL"):
    # Keep the existing private report shape, but use a past synthetic receipt for real CLI time.
    report = json.loads(
        json.dumps(_draft(ticker=ticker)).replace("2026-", "2025-").replace("2027-", "2026-")
    )
    if not comparable:
        report.update(official_close=None, comparison=None, unresolved=["取引所終値未確認。"])
    else:
        report["comparison"]["horizon_et"] = (
            horizon or (datetime.now(UTC) + timedelta(days=365)).date().isoformat()
        )
    authored, review = _prepare(tmp_path, capsys, report)
    review["reviewed_at"] = "2025-11-28T16:00:00Z"
    analysis = str(tmp_path / "analysis.json")
    assert (
        main(["save", authored, _write(tmp_path / "review.json", review), "--output", analysis])
        == 0
    )
    return analysis, {
        "proposal_id": "us-proposal-synthetic-only",
        "author_identity": "synthetic decision author",
        "prepared_at": "2025-11-28T17:00:00Z",
        "action": "no_action" if comparable else "defer",
        "reason": "現金に比べ下振れを引き受ける根拠が弱い。"
        if comparable
        else "公式終値が未確認。",
        "revisit_when": "取引所の価格と判断材料を再確認。",
    }


def _review_decision(analysis, decision_path, capsys):
    assert main(["proposal-digest", analysis, decision_path]) == 0
    return {
        **json.loads(capsys.readouterr().out),
        "reviewer_identity": "synthetic decision peer",
        "reviewed_at": "2025-11-28T18:00:00Z",
        "conclusion": "pass",
        "notes": "この判断の根拠と見直し条件を別に確認。",
    }


@pytest.mark.parametrize("comparable", [True, False])
def test_isolated_us_formal_decision_save_and_exact_readback(tmp_path, capsys, comparable):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=comparable, ticker="AAPL")
    decision_path = _write(tmp_path / "decision.json", decision)
    review = _review_decision(analysis, decision_path, capsys)
    review_path = _write(tmp_path / "decision-review.json", review)
    db = str(tmp_path / "app #isolated.sqlite")
    initialize_database(Path(db))
    assert main(["proposal-show", "--db", db]) == 1
    assert capsys.readouterr().out == "null\n"
    assert main(["proposal-save", analysis, decision_path, review_path, "--db", db]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved == {key: review[key] for key in ("proposal_id", "draft_sha256")}
    assert saved["draft_sha256"] != json.loads(Path(analysis).read_text())["review"]["draft_sha256"]

    before_read = Path(db).read_bytes()
    assert main(["proposal-show", "--db", db, "--proposal-id", saved["proposal_id"]]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["decision"] == decision
    assert observed["review"]["draft_sha256"] == saved["draft_sha256"]
    assert observed["analysis"] == json.loads(Path(analysis).read_text())
    assert observed["analysis"]["draft"]["currency"] == "USD"
    assert observed["analysis"]["draft"]["ticker"] == "AAPL"
    close = observed["analysis"]["draft"]["official_close"]
    assert (close["price_usd"] if close else None) == ("120" if comparable else None)
    assert main(["proposal-show", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out) == observed
    assert main(["proposal-show", "--db", db, "--proposal-id", "jp-proposal"]) == 1
    assert capsys.readouterr().out == "null\n"
    assert Path(db).read_bytes() == before_read

    assert main(["proposal-save", analysis, decision_path, review_path, "--db", db]) == 2
    assert capsys.readouterr().err == "FAIL US proposal save\n"
    assert Path(db).read_bytes() == before_read
    with closing(sqlite3.connect(db)) as connection:
        for table in ("investment_proposal", "operation_session", "ledger_event"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    ("changes", "comparable"),
    [
        ({"action": "buy"}, True),
        ({"quantity": 1}, True),
        ({"action": "no_action"}, False),
        ({"prepared_at": "2025-11-28T15:59:59Z"}, True),
        ({"revisit_when": " "}, True),
    ],
)
def test_proposal_draft_does_not_promote_missing_or_executable_input(
    tmp_path, capsys, changes, comparable
):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=comparable)
    decision.update(changes)
    decision_path = _write(tmp_path / "decision.json", decision)
    assert main(["proposal-digest", analysis, decision_path]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAIL US proposal draft\n"


def test_formal_review_cannot_be_replaced_by_analysis_review_or_stale_decision(tmp_path, capsys):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=True)
    decision_path = _write(tmp_path / "decision.json", decision)
    valid = _review_decision(analysis, decision_path, capsys)
    db = str(tmp_path / "app.sqlite")
    initialize_database(Path(db))
    before = Path(db).read_bytes()
    saved_report = json.loads(Path(analysis).read_text())
    for proposed, review in (
        (decision, saved_report["review"]),
        ({**decision, "action": "defer"}, valid),
        ({**decision, "revisit_when": "別の見直し条件"}, valid),
        (decision, {**valid, "reviewer_identity": " SYNTHETIC  DECISION\tAUTHOR "}),
        (decision, {**valid, "reviewed_at": "2099-11-28T16:00:00Z"}),
    ):
        _write(Path(decision_path), proposed)
        review_path = _write(tmp_path / "decision-review.json", review)
        assert main(["proposal-save", analysis, decision_path, review_path, "--db", db]) == 2
        assert capsys.readouterr().err == "FAIL US proposal save\n"
    assert Path(db).read_bytes() == before
    _write(Path(decision_path), decision)
    saved_report["rendered_sha256"] = "0" * 64
    _write(Path(analysis), saved_report)
    assert main(["proposal-digest", analysis, decision_path]) == 2
    assert capsys.readouterr().err == "FAIL US proposal draft\n"


def test_latest_us_proposal_normalizes_publication_clock_and_never_hides_invalid_latest(
    tmp_path, capsys
):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=False)
    evidence = ReviewedReport.model_validate_json(Path(analysis).read_bytes())
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    for ending, instant in (
        ("older", "2025-11-29T04:59:59.999999+09:00"),
        ("tied-z", "2025-11-28T20:00:00.000000+00:00"),
        ("tied-a", "2025-11-28T15:00:00.000000-05:00"),
    ):
        draft = USProposalDraft.model_validate({**decision, "proposal_id": f"us-proposal-{ending}"})
        review = USProposalReview(
            proposal_id=draft.proposal_id,
            draft_sha256=prepare_us_proposal(draft, evidence),
            reviewer_identity="independent peer",
            reviewed_at=datetime.fromisoformat("2025-11-28T18:00:00Z"),
            conclusion="pass",
            notes="independent review for this sample",
        )
        save_us_proposal(db, draft, evidence, review, published_at=datetime.fromisoformat(instant))
    assert main(["proposal-show", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["proposal_id"] == "us-proposal-tied-z"
    with closing(sqlite3.connect(db, autocommit=True)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE us_equity_proposal SET payload='{}' WHERE proposal_id=?",
                ("us-proposal-tied-z",),
            )
        # Inject a broken saved row below the application writer to check the read boundary.
        connection.execute("DROP TRIGGER us_equity_proposal_immutable")
        connection.execute(
            "UPDATE us_equity_proposal SET payload=json_set(payload, '$.decision.reason', 'changed') "
            "WHERE proposal_id=?",
            ("us-proposal-tied-z",),
        )
    assert main(["proposal-show", "--db", str(db)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "FAIL US App read\n"
    assert captured.out == ""
    assert main(["proposal-show", "--db", str(db), "--proposal-id", "us-proposal-tied-a"]) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_id", "us-report-wrong"),
        ("draft_sha256", "2" * 64),
        ("reviewer_identity", "SYNTHETIC  AUTHOR"),
        ("reviewed_at", "2025-11-28T14:59:59Z"),
    ],
)
def test_app_read_detects_an_invalid_nested_review_even_with_matching_outer_digest(
    tmp_path, capsys, field, value
):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=False)
    decision_path = _write(tmp_path / "decision.json", decision)
    review = _review_decision(analysis, decision_path, capsys)
    review_path = _write(tmp_path / "decision-review.json", review)
    db = tmp_path / "synthetic.sqlite"
    initialize_database(db)
    assert main(["proposal-save", analysis, decision_path, review_path, "--db", str(db)]) == 0
    capsys.readouterr()
    fake_id = "us-proposal-z-corrupt"
    # A valid outer hash alone does not make the nested report's review valid.
    with closing(sqlite3.connect(db, autocommit=True)) as connection:
        stamp, payload = connection.execute(
            "SELECT published_at, payload FROM us_equity_proposal"
        ).fetchone()
        corrupted = json.loads(payload)
        corrupted["decision"]["proposal_id"] = fake_id
        corrupted["analysis"]["review"][field] = value
        corrupted["review"]["proposal_id"] = fake_id
        corrupted["review"]["draft_sha256"] = sha256(
            canonical_json({key: corrupted[key] for key in ("decision", "analysis")}).encode()
        ).hexdigest()
        connection.execute(
            "INSERT INTO us_equity_proposal VALUES (?,?,?)",
            (fake_id, stamp, canonical_json(corrupted)),
        )

    for selector in ([], ["--proposal-id", fake_id]):
        assert main(["proposal-show", "--db", str(db), *selector]) == 2
        captured = capsys.readouterr()
        assert captured.err == "FAIL US App read\n"
        assert captured.out == ""
    assert main(["proposal-show", "--db", str(db), "--proposal-id", decision["proposal_id"]]) == 0


def test_no_action_horizon_uses_recorded_et_dates_instead_of_the_read_clock(tmp_path, capsys):
    report, decision = _decision_inputs(tmp_path, capsys, comparable=True, horizon="2025-12-01")
    analysis = ReviewedReport.model_validate_json(Path(report).read_bytes())
    draft = USProposalDraft.model_validate(decision)

    def review_for(draft):
        return USProposalReview(
            proposal_id=draft.proposal_id,
            draft_sha256=prepare_us_proposal(draft, analysis),
            reviewer_identity="synthetic peer",
            reviewed_at=datetime.fromisoformat("2025-11-28T18:00:00Z"),
            conclusion="pass",
            notes="reviewed the separate simulated decision",
        )

    review = review_for(draft)
    db = tmp_path / "isolated.sqlite"
    initialize_database(db)
    initial = db.read_bytes()
    horizon_start_et = datetime.fromisoformat("2025-12-01T05:00:00Z")
    with pytest.raises(ValueError, match="comparison horizon"):
        save_us_proposal(db, draft, analysis, review, published_at=horizon_start_et)
    assert db.read_bytes() == initial

    # It is Dec 1 UTC, but still Nov 30 ET. Reading it now never uses the current clock.
    before_et = horizon_start_et - timedelta(microseconds=1)
    save_us_proposal(db, draft, analysis, review, published_at=before_et)
    assert main(["proposal-show", "--db", str(db), "--proposal-id", draft.proposal_id]) == 0
    assert json.loads(capsys.readouterr().out)["review"]["draft_sha256"] == review.draft_sha256

    defer = USProposalDraft.model_validate(
        {**decision, "action": "defer", "proposal_id": "us-proposal-synthetic-deferred"}
    )
    save_us_proposal(db, defer, analysis, review_for(defer), published_at=horizon_start_et)
    assert main(["proposal-show", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["action"] == "defer"

    too_late = USProposalDraft.model_validate({**decision, "prepared_at": horizon_start_et})
    with pytest.raises(ValueError, match="comparison horizon"):
        prepare_us_proposal(too_late, analysis)


def test_saved_no_action_is_unreadable_if_its_recorded_publication_never_had_a_future_horizon(
    tmp_path, capsys
):
    report, decision = _decision_inputs(tmp_path, capsys, comparable=True, horizon="2025-12-01")
    analysis = ReviewedReport.model_validate_json(Path(report).read_bytes())
    recorded_horizon = datetime.fromisoformat("2025-12-01T05:00:00Z")

    def reviewed_decision(name):
        draft = USProposalDraft.model_validate({**decision, "proposal_id": name})
        review = USProposalReview(
            proposal_id=name,
            draft_sha256=prepare_us_proposal(draft, analysis),
            reviewer_identity="synthetic peer",
            reviewed_at=datetime.fromisoformat("2025-11-28T18:00:00Z"),
            conclusion="pass",
            notes="reviewed the separate simulated decision",
        )
        return draft, review

    db = tmp_path / "synthetic.sqlite"
    initialize_database(db)
    related, peer = reviewed_decision("us-proposal-valid-admitted")
    valid = save_us_proposal(
        db, related, analysis, peer, published_at=recorded_horizon - timedelta(microseconds=1)
    )
    invalid, peer = reviewed_decision("us-proposal-unreadable-at-publication")
    stamp = recorded_horizon.isoformat(timespec="microseconds")
    unsupported_row = {
        "decision": invalid.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
        "review": peer.model_dump(mode="json"),
        "published_at": stamp,
    }
    with closing(sqlite3.connect(db, autocommit=True)) as connection:
        connection.execute(
            "INSERT INTO us_equity_proposal VALUES (?,?,?)",
            (invalid.proposal_id, stamp, canonical_json(unsupported_row)),
        )

    before = db.read_bytes()
    assert main(["proposal-show", "--db", str(db), "--proposal-id", related.proposal_id]) == 0
    assert json.loads(capsys.readouterr().out) == valid.model_dump(mode="json")
    for selector in ([], ["--proposal-id", invalid.proposal_id]):
        assert main(["proposal-show", "--db", str(db), *selector]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "FAIL US App read\n"
    assert db.read_bytes() == before


def _finance_perspective():
    return {
        "model": "company_financials",
        "assessment": "mixed",
        "summary": "合成例：営業利益は増えたが営業現金収支は鈍い。",
        "sources": [
            {
                "label": "合成10-Q（2025年9月期）Item 2 営業現金収支",
                "observed_at": "2025-11-28T09:00:00-05:00",  # exactly the report cutoff
            }
        ],
    }


def test_formal_us_perspectives_are_independently_bound_before_web_materialization(
    tmp_path, capsys
):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=False)
    decision["model_perspectives"] = [
        _finance_perspective(),
        {
            "model": "external_assessment",
            "assessment": "insufficient",
            "summary": "合成例：対象期に対応する独立した予想は確認できなかった。",
            "sources": [],
        },
    ]
    decision_path = _write(tmp_path / "decision.json", decision)
    binding = _review_decision(analysis, decision_path, capsys)
    review_path = _write(tmp_path / "decision-review.json", binding)
    db = tmp_path / APPLICATION_DB_PATH
    initialize_database(db)
    # The peer signed the published decision, not just the private analysis or old wording.
    for replacement in (
        {**_finance_perspective(), "summary": "根拠の強さを訂正。"},
        {**_finance_perspective(), "assessment": "adverse"},
        {
            **_finance_perspective(),
            "sources": [{"label": "別資料", "observed_at": "2025-11-27T18:00:00Z"}],
        },
    ):
        changed = {
            **decision,
            "model_perspectives": [replacement, decision["model_perspectives"][1]],
        }
        _write(Path(decision_path), changed)
        assert main(["proposal-save", analysis, decision_path, review_path, "--db", str(db)]) == 2
        assert capsys.readouterr().err == "FAIL US proposal save\n"
    assert main(["proposal-show", "--db", str(db)]) == 1
    assert capsys.readouterr().out == "null\n"

    _write(Path(decision_path), decision)
    assert main(["proposal-save", analysis, decision_path, review_path, "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["draft_sha256"] == binding["draft_sha256"]
    assert main(["proposal-show", "--db", str(db), "--proposal-id", decision["proposal_id"]]) == 0
    stored = json.loads(capsys.readouterr().out)
    expected = json.loads(json.dumps(decision["model_perspectives"]))
    expected[0]["sources"][0]["observed_at"] = "2025-11-28T14:00:00Z"
    assert stored["decision"]["model_perspectives"] == expected
    assert stored["analysis"] == json.loads(Path(analysis).read_bytes())
    out = tmp_path / "served"
    export_read_models(tmp_path, out)
    payload = (out / "views/latest-us-proposal.json").read_text(encoding="utf-8")
    served = json.loads(payload)["proposal"]
    assert served["draft_sha256"] == binding["draft_sha256"]
    assert served["action"] == "defer"
    assert served["official_close"] is None
    # No unsupplied row or direction is inferred, even though the IEX band exists.
    assert served["model_perspectives"] == expected
    for private in ("https://", "合成の収益継続仮説", "synthetic decision peer"):
        assert private not in payload


@pytest.mark.parametrize(
    ("row", "duplicate"),
    [
        ({"model": "other"}, False),
        ({}, True),
        ({"assessment": "neutral", "sources": []}, False),
        ({"summary": "字" * 481}, False),
        ({"sources": [{"label": "字" * 181, "observed_at": "2025-11-27T12:00:00Z"}]}, False),
        ({"sources": [{"label": "合成資料", "observed_at": "2025-11-27T12:00:00Z"}] * 5}, False),
        ({"assessment": "not_applicable", "sources": [], "summary": " "}, False),
        ({"sources": [{"label": "late", "observed_at": "2025-11-28T14:00:00.000001Z"}]}, False),
        (
            {"sources": [{"label": "cutoff+1ns", "observed_at": "2025-11-28T14:00:00.000000001Z"}]},
            False,
        ),
        ({"sources": [{"label": "date only", "observed_at": "2025-11-27"}]}, False),
        (
            {"sources": [{"label": "unknown offset", "observed_at": "2025-11-27T12:00:00-00:00"}]},
            False,
        ),
        ({"sources": [{"label": "epoch not a read clock", "observed_at": 0}]}, False),
        (
            {
                "sources": [
                    {"label": "https://example.com/private", "observed_at": "2025-11-27T12:00:00Z"}
                ]
            },
            False,
        ),
        (
            {
                "sources": [
                    {
                        "label": "合成資料",
                        "observed_at": "2025-11-27T12:00:00Z",
                        "url": "https://example.com/private",
                    }
                ]
            },
            False,
        ),
    ],
)
def test_invalid_perspective_never_creates_a_reviewable_decision(tmp_path, capsys, row, duplicate):
    analysis, decision = _decision_inputs(tmp_path, capsys, comparable=False)
    rows = [{**_finance_perspective(), **row}]
    decision["model_perspectives"] = rows * (2 if duplicate else 1)
    decision_path = _write(tmp_path / "invalid.json", decision)
    assert main(["proposal-digest", analysis, decision_path]) == 2
    capture = capsys.readouterr()
    assert (capture.out, capture.err) == ("", "FAIL US proposal draft\n")


def test_programmatic_source_offset_seconds_round_trips_at_exact_cutoff(tmp_path, capsys):
    report, decision = _decision_inputs(tmp_path, capsys, comparable=False)
    analysis = ReviewedReport.model_validate_json(Path(report).read_bytes())
    row = _finance_perspective()
    offset = timezone(-timedelta(seconds=30))
    instant = analysis.draft.cutoff_at
    source = row["sources"][0]
    source["observed_at"] = instant.astimezone(offset)
    draft = USProposalDraft.model_validate({**decision, "model_perspectives": [row]})
    assert draft.model_perspectives[0].sources[0].observed_at == instant
    serialized = USProposalDraft.model_validate_json(draft.model_dump_json())
    assert prepare_us_proposal(serialized, analysis) == prepare_us_proposal(draft, analysis)
    source["observed_at"] = (instant + timedelta(microseconds=1)).astimezone(offset)
    late = USProposalDraft.model_validate({**decision, "model_perspectives": [row]})
    with pytest.raises(ValueError, match="report cutoff"):
        prepare_us_proposal(late, analysis)


def test_unrepresentable_perspective_time_is_a_validation_error():
    row = _finance_perspective()
    row["sources"][0]["observed_at"] = "0001-01-01T00:00:00+09:00"
    with pytest.raises(ValidationError, match="observation time outside representable UTC range"):
        USProposalDraft.model_validate(
            {
                "proposal_id": "us-proposal-invalid-source",
                "author_identity": "synthetic author",
                "prepared_at": "2025-11-28T17:00:00Z",
                "action": "defer",
                "reason": "合成理由",
                "revisit_when": "合成条件",
                "model_perspectives": [row],
            }
        )


@pytest.mark.parametrize(
    ("owner", "clock"),
    [
        ("iex_band", "received_at"),
        ("official_close", "observed_at"),
        ("financial_evidence", "observed_at"),
    ],
)
def test_report_evidence_does_not_round_receipts_into_cutoff(tmp_path, capsys, owner, clock):
    payload = _draft()
    source = payload[owner][0] if owner == "financial_evidence" else payload[owner]
    path = tmp_path / "draft.json"

    # The ordinary CLI receives the original text, before Pydantic can truncate it.
    source[clock] = "2026-11-28T09:00:00.000000001-05:00"
    assert main(["draft-digest", _write(path, payload)]) == 2
    assert capsys.readouterr().out == ""
    source[clock] = "2026-11-28T09:00:00.000000000-05:00"
    assert main(["draft-digest", _write(path, payload)]) == 0
    assert json.loads(capsys.readouterr().out)["report_id"] == payload["report_id"]


def test_report_uses_instants_across_both_occurrences_of_a_new_york_hour():
    payload = _draft()
    ny = ZoneInfo("America/New_York")
    cutoff = datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=0)
    payload.update(
        cutoff_at=cutoff,
        expected_session_et="2026-10-30",
        session_close_at="2026-10-30T16:00:00-04:00",
        iex_band=None,
        official_close=None,
        comparison=None,
        unresolved=["合成。価格・比較は未確認。"],
    )
    fact = payload["financial_evidence"][0]
    # 01:29 EST is 59 minutes *after* 01:30 EDT.
    fact["observed_at"] = cutoff.replace(minute=29, fold=1)
    with pytest.raises(ValidationError, match="financial evidence is unavailable at cutoff"):
        ReportDraft.model_validate(payload)
    fact["observed_at"] = cutoff
    draft = ReportDraft.model_validate(payload)
    assert draft.model_dump(mode="json")["cutoff_at"] == "2026-11-01T01:30:00-04:00"
    assert ReportDraft.model_validate_json(draft.model_dump_json()) == draft


def test_financial_evidence_accepts_the_first_representable_exchange_local_instant():
    payload = _draft()
    first_et = datetime.min.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
    fact = payload["financial_evidence"][0]
    fact.update(period_end="0001-01-01", observed_at=first_et.isoformat())
    assert ReportDraft.model_validate(payload).financial_evidence[0].observed_at == first_et
    fact["observed_at"] = (first_et - timedelta(microseconds=1)).isoformat()
    with pytest.raises(ValidationError, match="representable New York date"):
        ReportDraft.model_validate(payload)
