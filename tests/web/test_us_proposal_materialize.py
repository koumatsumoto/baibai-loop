"""Synthetic App projection: exact USD identity without widening the JP views."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from hashlib import sha256

import pytest
from tests.helpers.outcome_publication import _publication
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
    read_nasdaq_official_close,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReportReview,
    SipCloseProof,
    USProposalDraft,
    USProposalReview,
    draft_digest,
    prepare_us_proposal,
    reviewed_report,
    save_us_proposal,
    sip_official_close_fields,
)
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.paths import APPLICATION_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.contracts_export import build_schema
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders import dashboard
from baibai_web.sources import proposal as proposal_source

PRICE_CLOCK = datetime.fromisoformat("2026-06-03T13:00:00Z")
PUBLISHED_US = datetime.fromisoformat("2026-06-03T17:00:00Z")
STAMP = max(PUBLISHED, PUBLISHED_US) + timedelta(hours=4)
JP_OBJECTS = {
    "portfolio-dashboard.json",
    "outcomes.json",
    "latest-proposal.json",
    "proposal-history.json",
}


def _save_us(
    app,
    proposal_id,
    *,
    comparable=True,
    action=None,
    precise=False,
    published_at=PUBLISHED_US,
    sip_proof=None,
):
    action = action or ("no_action" if comparable else "defer")
    official_close = {
        "basis": "regular_session_unadjusted",
        "session_et": "2026-06-02",
        "price_usd": "100.50",
        "source_url": "https://www.nasdaq.com/synthetic-official",
        "observed_at": PRICE_CLOCK,
    }
    if sip_proof is not None:
        official_close = sip_official_close_fields(
            read_nasdaq_official_close(
                sip_proof.query,
                sip_proof.receipt,
                listing_venue="XNAS",
                session_et=date(2026, 6, 2),
                session_close_at=datetime.fromisoformat("2026-06-02T16:00:00-04:00"),
                cutoff_at=datetime.fromisoformat("2026-06-03T15:00:00Z"),
            )
        )
    scenario = {
        "terminal_value_usd_per_share": "100.50" if precise else "105.75",
        "eligible_distributions_usd_per_share": "1.0E-13" if precise else "1.25",
        "assumptions": ["synthetic comparison; not a Web input"],
    }
    draft = ReportDraft.model_validate(
        {
            "kind": "us_stock_analytical_report",
            "report_id": "us-report-us-web-fixture",
            "author_identity": "synthetic analysis author",
            "ticker": "GOOGL",
            "cik": "0001652044",
            "venue": "XNAS",
            "share_class": "Class A",
            "currency": "USD",
            "cutoff_at": "2026-06-03T15:00:00Z",
            "prepared_at": "2026-06-03T16:00:00Z",
            "expected_session_et": "2026-06-02",
            "session_close_at": "2026-06-02T16:00:00-04:00",
            "calendar_source_url": "https://www.nasdaq.com/synthetic-calendar",
            "iex_band": {
                "feed": "alpaca_iex",
                "basis": "split_adjusted_closes",
                "manifest_sha256": "1" * 64,
                "received_at": PRICE_CLOCK,
                "first_session_et": "2026-05-20",
                "last_session_et": "2026-06-02",
                "observations": 10,
                "low_usd": "99.50",
                "high_usd": "130.12",
                "last_usd": "118.40",
            },
            "official_close": official_close if comparable else None,
            "financial_evidence": [
                {
                    "source_url": "https://www.sec.gov/synthetic-filing",
                    "period_end": "2026-03-31",
                    "observed_at": PRICE_CLOCK,
                    "fact_and_scope": "synthetic filing, not a Web input",
                }
            ],
            "thesis": "synthetic analysis thesis, not a Web input",
            "strongest_countercase": "synthetic model risk, not a Web input",
            "comparison": {
                "horizon_et": "2027-05-31",
                "cash_basis": "same_usd_no_interest_tax_or_fees",
                "base": scenario,
                "downside": {
                    **scenario,
                    "terminal_value_usd_per_share": "80",
                    "eligible_distributions_usd_per_share": "0E-14" if precise else "0",
                },
            }
            if comparable
            else None,
            "unresolved": ["取引所終値未確認。"] if not comparable else [],
        }
    )
    report_review = ReportReview.model_validate(
        {
            "report_id": draft.report_id,
            "draft_sha256": draft_digest(draft),
            "reviewer_identity": "synthetic analysis peer",
            "reviewed_at": "2026-06-03T16:10:00Z",
            "conclusion": "pass",
            "notes": "synthetic analysis notes, not a Web input",
        }
    )
    analysis = reviewed_report(draft, report_review, sip_proof=sip_proof)
    decision = USProposalDraft.model_validate(
        {
            "proposal_id": proposal_id,
            "author_identity": "synthetic formal author",
            "prepared_at": "2026-06-03T16:20:00Z",
            "action": action,
            "reason": (
                "現金比較で見送り。"
                if action == "no_action"
                else "事業前提の未確認が残るため保留。"
                if comparable
                else "取引所終値未確認のため保留。"
            ),
            "revisit_when": "一次資料と価格を確認する。",
        }
    )
    review = USProposalReview.model_validate(
        {
            "proposal_id": proposal_id,
            "draft_sha256": prepare_us_proposal(decision, analysis, sip_proof=sip_proof),
            "reviewer_identity": "synthetic formal peer",
            "reviewed_at": "2026-06-03T16:30:00Z",
            "conclusion": "pass",
            "notes": "synthetic formal notes, not a Web input",
        }
    )
    return save_us_proposal(
        app, decision, analysis, review, published_at=published_at, sip_proof=sip_proof
    )


def _export(root, out):
    return {path.name: path.read_bytes() for path in export_read_models(root, out)}


def test_projection_uses_exact_formal_proof_no_hidden_body_or_legacy_change(tmp_path, mocker):
    # Existing independently generated Japanese outputs include a real synthetic
    # proposal, portfolio and outcomes. Their serialized bytes must not change.
    case = backend_case(tmp_path)
    japan = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    app = tmp_path / APPLICATION_DB_PATH
    app.parent.mkdir(parents=True, exist_ok=True)
    case.app.rename(app)
    PortfolioOutcomeStore(app).publish(_publication())
    mocker.patch.object(proposal_source, "datetime").now.return_value = STAMP
    mocker.patch.object(dashboard, "datetime").now.return_value = STAMP
    before = _export(tmp_path, tmp_path / "before")
    assert json.loads(before["latest-us-proposal.json"]) == {
        "generated_at": STAMP.isoformat(),
        "status": "empty",
        "error": None,
        "proposal": None,
    }

    saved = _save_us(app, "us-proposal-view-synthetic")
    original_app_bytes = app.read_bytes()
    after = _export(tmp_path, tmp_path / "after")
    assert before.keys() == after.keys() == {*JP_OBJECTS, "latest-us-proposal.json"}
    assert {name: after[name] for name in JP_OBJECTS} == {name: before[name] for name in JP_OBJECTS}
    assert json.loads(after["latest-proposal.json"])["proposal"]["proposal_id"] == japan.proposal_id
    output = json.loads(after["latest-us-proposal.json"])
    assert output["status"] == "ready"
    assert output["error"] is None
    view = output["proposal"]
    assert view == {
        "proposal_id": saved.decision.proposal_id,
        "draft_sha256": saved.review.draft_sha256,
        "ticker": "GOOGL",
        "venue": "XNAS",
        "share_class": "Class A",
        "currency": "USD",
        "session_et": "2026-06-02",
        "cutoff_at": "2026-06-03T15:00:00Z",
        "published_at": "2026-06-03T17:00:00Z",
        "reviewed_at": "2026-06-03T16:30:00Z",
        "action": "no_action",
        "reason": "現金比較で見送り。",
        "revisit_when": "一次資料と価格を確認する。",
        "official_close": {
            "price_usd": "100.50",
            "origin": "listing_exchange",
            "observed_at": "2026-06-03T13:00:00Z",
        },
        "iex_band": {
            "first_session_et": "2026-05-20",
            "last_session_et": "2026-06-02",
            "observations": 10,
            "low_usd": "99.50",
            "high_usd": "130.12",
            "last_usd": "118.40",
        },
        "comparison_horizon_et": "2027-05-31",
        "comparison": {
            "cash_basis": "same_usd_no_interest_tax_or_fees",
            "base": {
                "terminal_value_usd_per_share": "105.75",
                "eligible_distributions_usd_per_share": "1.25",
                "difference_from_cash_usd_per_share": "6.50",
            },
            "downside": {
                "terminal_value_usd_per_share": "80",
                "eligible_distributions_usd_per_share": "0",
                "difference_from_cash_usd_per_share": "-20.50",
            },
        },
        "unresolved": [],
    }
    assert app.read_bytes() == original_app_bytes


@pytest.mark.parametrize("action", ["no_action", "defer"])
def test_projection_keeps_verified_sip_origin_distinct_and_omits_the_raw_receipt(
    tmp_path, mocker, action
):
    app = tmp_path / APPLICATION_DB_PATH
    initialize_database(app)
    mocker.patch.object(proposal_source, "datetime").now.return_value = STAMP
    query = NasdaqOfficialCloseQuery(
        "GOOGL",
        datetime.fromisoformat("2026-06-02T19:55:00Z"),
        datetime.fromisoformat("2026-06-02T21:00:00Z"),
    )
    original = b"""{
      "symbol":"GOOGL","next_page_token":null,"auctions":[{
      "d":"2026-06-02","o":null,"c":[
        {"x":"Q","c":"6","p":117.31,"t":"2026-06-02T20:00:00.221350239Z"},
        {"x":"Q","c":"M","p":100.5000,"t":"2026-06-02T20:00:00.221350240Z"}
      ]}]
    }"""
    digest = sha256(original).hexdigest()
    proof = SipCloseProof(
        query,
        SipAuctionReceipt(
            query.url(), query.url(), 200, PRICE_CLOCK, digest, True, False, original
        ),
    )
    precise = action == "defer"
    saved = _save_us(
        app, "us-proposal-synthetic-sip-view", action=action, precise=precise, sip_proof=proof
    )
    before = app.read_bytes()
    raw = _export(tmp_path, tmp_path / "served")["latest-us-proposal.json"]
    view = json.loads(raw)
    assert view["status"] == "ready"
    proposal = view["proposal"]
    assert proposal["proposal_id"] == saved.decision.proposal_id
    assert proposal["draft_sha256"] == saved.review.draft_sha256
    assert proposal["currency"] == "USD"
    assert proposal["official_close"] == {
        "origin": "alpaca_sip_nasdaq_official_close",
        "price_usd": "100.5000",
        "observed_at": "2026-06-03T13:00:00Z",
    }
    assert proposal["iex_band"]["last_usd"] == "118.40"
    assert proposal["comparison_horizon_et"] == "2027-05-31"
    assert proposal["action"] == action
    assert proposal["comparison"] == {
        "cash_basis": "same_usd_no_interest_tax_or_fees",
        "base": {
            "terminal_value_usd_per_share": "100.50" if precise else "105.75",
            "eligible_distributions_usd_per_share": "1.0E-13" if precise else "1.25",
            "difference_from_cash_usd_per_share": "1.0E-13" if precise else "6.5000",
        },
        "downside": {
            "terminal_value_usd_per_share": "80",
            "eligible_distributions_usd_per_share": "0E-14" if precise else "0",
            "difference_from_cash_usd_per_share": "-20.50000000000000" if precise else "-20.5000",
        },
    }
    assert digest.encode() not in raw
    assert b"alpaca.markets" not in raw
    assert b"221350240" not in raw
    assert b"/auctions/" not in raw
    assert app.read_bytes() == before


@pytest.mark.parametrize(
    ("comparable", "action"), [(True, "defer"), (False, "defer"), (True, "no_action")]
)
def test_saved_comparison_is_not_computed_from_action_or_todays_price(tmp_path, comparable, action):
    app = tmp_path / APPLICATION_DB_PATH
    app.parent.mkdir(parents=True, exist_ok=True)
    initialize_database(app)
    saved = _save_us(
        app, "us-proposal-saved-basis", comparable=comparable, action=action, precise=True
    )
    before = app.read_bytes()
    # A historic comparison is evidence from the saved decision, not today's eligibility.
    _, us = proposal_source.read_serving_proposal_views(
        app, generated_at=datetime.fromisoformat("2028-01-01T01:00:00Z")
    )
    assert us.proposal is not None
    assert us.proposal.action == action
    assert us.proposal.draft_sha256 == saved.review.draft_sha256
    view = us.proposal.model_dump(mode="json")
    if comparable:
        assert view["comparison_horizon_et"] == "2027-05-31"
        comparison = view["comparison"]
        assert comparison["base"] == {
            "terminal_value_usd_per_share": "100.50",
            "eligible_distributions_usd_per_share": "1.0E-13",
            "difference_from_cash_usd_per_share": "1.0E-13",
        }
        assert comparison["downside"]["eligible_distributions_usd_per_share"] == "0E-14"
        defs = build_schema()["$defs"]
        properties = defs["USProposalComparisonScenarioView"]["properties"]
        for name in ("base", "downside"):
            for field, value in comparison[name].items():
                schema = properties[field]
                if ref := schema.get("$ref"):
                    schema = defs[ref.rsplit("/", 1)[-1]]
                assert re.fullmatch(schema["pattern"], value)
    else:
        assert view["official_close"] is None
        assert view["iex_band"]["last_usd"] == "118.40"
        assert view["comparison_horizon_et"] is None
        assert view["comparison"] is None
    assert app.read_bytes() == before


def test_proposals_share_snapshot_even_with_a_concurrent_us_append(tmp_path, mocker, monkeypatch):
    app = tmp_path / "isolated.sqlite"
    initialize_database(app)
    with closing(sqlite3.connect(app)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    first = _save_us(app, "us-proposal-first", comparable=False)
    connect = mocker.spy(proposal_source, "connect_read_only")
    read_us = proposal_source.read_us_proposal_in_snapshot

    def append_after_jp_read(connection, *, generated_at):
        assert connection.in_transaction
        _save_us(app, "us-proposal-second", published_at=PUBLISHED_US + timedelta(minutes=1))
        return read_us(connection, generated_at=generated_at)

    with monkeypatch.context() as during_snapshot:
        during_snapshot.setattr(
            proposal_source, "read_us_proposal_in_snapshot", append_after_jp_read
        )
        jp, us = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert connect.call_count == 1
    assert jp.latest.generated_at == jp.history.generated_at == us.generated_at == STAMP
    assert us.status == "ready"
    assert us.proposal is not None
    assert us.proposal.proposal_id == first.decision.proposal_id
    assert us.proposal.action == "defer"
    assert us.proposal.official_close is None
    assert us.proposal.comparison_horizon_et is None
    assert us.proposal.comparison is None
    _, following = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert following.proposal is not None
    assert following.proposal.proposal_id == "us-proposal-second"


@pytest.mark.parametrize("damaged", ["us", "jp", "missing", "wrong-version"])
def test_unavailable_is_not_empty_or_older_fallback(tmp_path, damaged):
    app = tmp_path / "isolated.sqlite"
    if damaged != "missing":
        initialize_database(app)
        _save_us(app, "us-proposal-older")
        with closing(sqlite3.connect(app, autocommit=True)) as db:
            if damaged == "us":
                db.execute(
                    "INSERT INTO us_equity_proposal VALUES ('us-proposal-damaged', ?, '{}')",
                    ("2026-06-04T00:00:00.000000+00:00",),
                )
            elif damaged == "jp":
                db.execute("DROP TABLE investment_proposal")
            else:
                db.execute("PRAGMA user_version=30")
    before = app.read_bytes() if app.exists() else None
    jp, us = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    if damaged == "jp":
        assert us.status == "ready"
        assert us.proposal is not None
        assert us.proposal.proposal_id == "us-proposal-older"
    else:
        assert us.status == "unavailable"
        assert us.error == "保存済みの米国株判断を読み取れません。"
        assert us.proposal is None
    assert jp.latest.status == ("empty" if damaged == "us" else "unavailable")
    assert (app.read_bytes() if app.exists() else None) == before
