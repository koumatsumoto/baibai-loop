"""Synthetic App projection: exact USD identity without widening the JP views."""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import get_args

import pytest
from tests.helpers.outcome_publication import _publication
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
    read_nasdaq_official_close,
)
from baibai_engine.investment_judgment.contracts import PerspectiveAssessment, PerspectiveModel
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReportReview,
    SipCloseProof,
    USProposalDraft,
    USProposalReview,
    draft_digest,
    prepare_us_proposal,
    published_us_proposal_ids,
    reviewed_report,
    save_us_proposal,
    sip_official_close_fields,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    encode_current_listing_master,
)
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.primitives.time import JST
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web import materialize
from baibai_web.contracts_export import build_schema
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders import dashboard
from baibai_web.readmodel.proposal_models import SavedModelPerspectiveView
from baibai_web.readmodel.us_proposal_models import USProposalPerspectiveView
from baibai_web.sources import db_sources
from baibai_web.sources import proposal as proposal_source
from baibai_web.sources import us_proposal as us_source

PRICE_CLOCK = datetime.fromisoformat("2026-06-03T13:00:00Z")
PUBLISHED_US = datetime.fromisoformat("2026-06-03T17:00:00Z")
STAMP = max(PUBLISHED, PUBLISHED_US) + timedelta(hours=4)
JP_OBJECTS = {
    "portfolio-dashboard.json",
    "outcomes.json",
    "latest-proposal.json",
    "proposal-history.json",
}


def test_public_perspective_schemas_match_the_formal_decision_vocabulary() -> None:
    expected = {"model": get_args(PerspectiveModel), "assessment": get_args(PerspectiveAssessment)}
    for view in (SavedModelPerspectiveView, USProposalPerspectiveView):
        properties = view.model_json_schema()["properties"]
        for name, values in expected.items():
            assert properties[name]["enum"] == list(values)


def _save_us(
    app,
    proposal_id,
    *,
    comparable=True,
    action=None,
    precise=False,
    published_at=PUBLISHED_US,
    sip_proof=None,
    perspectives=(),
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
            "model_perspectives": perspectives,
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


@pytest.mark.parametrize(
    "perspectives",
    [
        pytest.param([], id="unrecorded"),
        pytest.param(
            [
                {
                    "model": "market_context",
                    "assessment": "insufficient",
                    "summary": "東京寄り前の米国市場全体の動きは未確認。",
                    "sources": [],
                }
            ],
            id="preopen-unavailable",
        ),
    ],
)
def test_projection_uses_exact_formal_proof_no_hidden_body_or_legacy_change(
    tmp_path, mocker, perspectives
):
    # Existing independently generated Japanese outputs include a real synthetic
    # proposal, portfolio and outcomes. Their serialized bytes must not change.
    case = backend_case(tmp_path)
    japan = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
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
        "history": {
            "status": "empty",
            "error": None,
            "proposals": [],
            "unavailable_proposal_ids": [],
            "has_more": False,
        },
    }

    saved = _save_us(app, "us-proposal-view-synthetic", perspectives=perspectives)
    # Byte-for-byte pre-perspective fixture binding: no manufactured judgement on old records.
    if not perspectives:
        assert saved.review.draft_sha256 == (
            "4726d2a668171d06d12b4567942f0915de31d62db48dacb7ce42fa29954b33b4"
        )
    original_app_bytes = app.read_bytes()
    after = _export(tmp_path, tmp_path / "after")
    assert before.keys() == after.keys() == {*JP_OBJECTS, "latest-us-proposal.json"}
    assert {name: after[name] for name in JP_OBJECTS} == {name: before[name] for name in JP_OBJECTS}
    assert json.loads(after["latest-proposal.json"])["proposal"]["proposal_id"] == japan.proposal_id
    output = json.loads(after["latest-us-proposal.json"])
    assert output["status"] == "ready"
    assert output["error"] is None
    assert output["history"] == {
        "status": "ready",
        "error": None,
        "proposals": [],
        "unavailable_proposal_ids": [],
        "has_more": False,
    }
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
        "model_perspectives": perspectives,
        "comparison_price": {
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


@pytest.mark.parametrize(
    "state",
    [
        "current",
        "closed_jst",
        "absent",
        "corrupt",
        "stale",
        "late",
        "unverified",
        "wrong_class",
        "invalid",
    ],
)
def test_pinned_current_jp_master_changes_only_the_held_listing_display_name(
    tmp_path, mocker, caplog, state
):
    case = backend_case(tmp_path, holding=True)
    jp = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case, action="hold"))
    us = _save_us(case.app, "us-proposal-unaffected-by-current-jp-master")
    PortfolioOutcomeStore(case.app).publish(_publication())
    if state == "closed_jst":
        # 00:30 Saturday in Tokyo, yet still Friday in UTC.
        now = datetime(2026, 10, 16, 15, 30, tzinfo=UTC)
        session = date(2026, 10, 16)
        seed_daily_bars(
            case.market,
            [("1234", day, 1100.0, 1.0) for day in ("2026-10-15", "2026-10-16")],
        )
    else:
        now = STAMP
        session = now.astimezone(JST).date()
    mocker.patch.object(proposal_source, "datetime").now.return_value = now
    mocker.patch.object(dashboard, "datetime").now.return_value = now
    app, market = tmp_path / APPLICATION_DB_PATH, tmp_path / MARKET_DB_PATH
    for source, target in ((case.app, app), (case.market, market)):
        target.parent.mkdir(parents=True)
        source.rename(target)

    legacy = _export(tmp_path, tmp_path / "legacy")
    expected = json.loads(legacy["portfolio-dashboard.json"])
    assert expected["holdings"][0]["company_name"] == "合成企業"
    assert expected["holdings"][0]["quantity"] == 100
    assert expected["holdings"][0]["market_value_yen"] == 110_000
    assert json.loads(legacy["latest-proposal.json"])["proposal"]["proposal_id"] == jp.proposal_id
    assert (
        json.loads(legacy["latest-us-proposal.json"])["proposal"]["proposal_id"]
        == us.decision.proposal_id
    )
    untouched = app.read_bytes(), market.read_bytes()
    # A stale legacy name exists in the same SQLite. The new path must never query it.
    legacy_call = mocker.patch.object(
        db_sources, "read_master_snapshot", side_effect=AssertionError("legacy name fallback")
    )
    current_reader = mocker.spy(db_sources, "read_current_jp_master")
    row = ListingMasterRow(
        ("XTKS", "12340"),
        "新しい現行表示名",
        "domestic_share",
        "listed",
        japan=JapanListingAttributes("011", "プライム", "卸売業"),
    )
    by_state = {
        "unverified": replace(row, listing_state="unverified"),
        "wrong_class": replace(row, key=("XTKS", "12345")),
        "invalid": replace(row, security_name=""),
    }
    section = CurrentListingMarket(
        ListingSlice(
            "JP",
            session - timedelta(days=1) if state == "stale" else session,
            (by_state.get(state, row),),
        ),
        source="jquants-jpx",
        received_at=now + (timedelta(seconds=1) if state == "late" else -timedelta(minutes=15)),
        received_count=1,
    )
    path = tmp_path / "SYNTHETIC_PRIVATE_MASTER.json"
    if state == "corrupt":
        path.write_text("{SYNTHETIC_PRIVATE_BODY")
    elif state != "absent":
        path.write_bytes(encode_current_listing_master({"JP": section}))
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "engine/src/baibai_engine").mkdir(parents=True)
    output = tmp_path / "current"
    read_file = mocker.spy(materialize, "read_local_listing_master")
    assert (
        materialize.main(
            [
                "--root",
                str(tmp_path),
                "--output-dir",
                str(output),
                "--current-listing-master",
                str(path),
            ]
        )
        == 0
    )
    read_file.assert_called_once_with(path)
    current = {view.name: view.read_bytes() for view in (output / "views").glob("*.json")}
    assert "SYNTHETIC_PRIVATE" not in caplog.text

    current_dashboard = json.loads(current.pop("portfolio-dashboard.json"))
    expected["holdings"][0]["company_name"] = (
        row.security_name if state in {"current", "closed_jst"} else None
    )
    assert current_dashboard == expected
    del legacy["portfolio-dashboard.json"]
    assert current == legacy  # Both markets' saved proposal IDs/bodies and outcomes are exact.
    assert (app.read_bytes(), market.read_bytes()) == untouched
    legacy_call.assert_not_called()
    if state in {"absent", "corrupt"}:
        current_reader.assert_not_called()
    else:
        current_reader.assert_called_once()
        assert current_reader.call_args.kwargs["evaluation_time"] == now
        assert current_reader.call_args.kwargs["tickers"] == ("1234",)


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
    assert proposal["comparison_price"] == {
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
        assert view["comparison_price"] is None
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
    assert us.proposal.comparison_price is None
    assert us.proposal.comparison_horizon_et is None
    assert us.proposal.comparison is None
    assert us.history.status == "ready"
    assert us.history.proposals == []
    _, following = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert following.proposal is not None
    assert following.proposal.proposal_id == "us-proposal-second"
    assert [row.proposal_id for row in following.history.proposals] == [
        first.decision.proposal_id,
    ]


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
        assert us.history.status == "ready"
        assert not us.history.proposals
    else:
        assert us.status == "unavailable"
        assert us.error == "保存済みの米国株判断を読み取れません。"
        assert us.proposal is None
        if damaged == "us":
            assert us.history.status == "partial"
            assert us.history.unavailable_proposal_ids == ["us-proposal-damaged"]
            assert [row.proposal_id for row in us.history.proposals] == ["us-proposal-older"]
            assert us.history.has_more is False
        else:
            assert us.history.status == "unavailable"
            assert us.history.has_more is None
    assert jp.latest.status == ("empty" if damaged == "us" else "unavailable")
    assert (app.read_bytes() if app.exists() else None) == before


@pytest.mark.parametrize(
    "json_path",
    [
        "$.analysis.draft.cutoff_at",
        "$.analysis.draft.iex_band.received_at",
        "$.analysis.draft.financial_evidence[0].observed_at",
    ],
)
def test_unrepresentable_exchange_date_does_not_hide_readable_us_history(tmp_path, json_path):
    app = tmp_path / "isolated.sqlite"
    initialize_database(app)
    older = _save_us(app, "us-proposal-older")
    # Corrupt only the synthetic latest slot. The older saved decision stays readable.
    with closing(sqlite3.connect(app, autocommit=True)) as db:
        db.execute(
            "INSERT INTO us_equity_proposal SELECT ?,published_at,"
            "json_set(payload,'$.decision.proposal_id',?,'$.review.proposal_id',?,?,?) "
            "FROM us_equity_proposal WHERE proposal_id=?",
            (
                "us-proposal-z",
                "us-proposal-z",
                "us-proposal-z",
                json_path,
                "0001-01-01T00:00:00Z",
                older.decision.proposal_id,
            ),
        )
    jp, us = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert jp.latest.status == "empty"
    assert us.status == "unavailable"
    assert us.history.status == "partial"
    assert us.history.unavailable_proposal_ids == ["us-proposal-z"]
    assert [row.proposal_id for row in us.history.proposals] == [older.decision.proposal_id]


def test_us_history_bounds_publication_slots_before_reading_exact_details(tmp_path, mocker):
    app = tmp_path / "window.sqlite"
    initialize_database(app)
    ids = [f"us-proposal-history-{index:02}" for index in range(22)]
    for index, proposal_id in enumerate(ids):
        published_at = PUBLISHED_US + timedelta(microseconds=index // 2)
        if index % 2 == 1:
            published_at = published_at.astimezone(PUBLISHED.tzinfo)
        _save_us(app, proposal_id, published_at=published_at)
    with closing(sqlite3.connect(app, autocommit=True)) as db:
        db.execute("DROP TRIGGER us_equity_proposal_immutable")
        db.execute("UPDATE us_equity_proposal SET payload='{}' WHERE proposal_id=?", (ids[20],))
    before = app.read_bytes()
    ids_read = mocker.spy(us_source, "published_us_proposal_ids")
    exact = mocker.spy(us_source, "load_us_proposal")
    generated_at = datetime.fromisoformat("2028-01-01T00:00:00Z")
    _, us = proposal_source.read_serving_proposal_views(app, generated_at=generated_at)
    assert ids_read.call_args.kwargs == {"limit": 21}
    assert [call.args[1] for call in exact.call_args_list] == ids[20:1:-1]
    assert all(call.args[0] is ids_read.call_args.args[0] for call in exact.call_args_list)
    assert us.status == "ready"
    assert us.proposal is not None
    assert us.proposal.proposal_id == ids[21]
    assert (
        us.proposal.published_at.isoformat()
        == (PUBLISHED_US + timedelta(microseconds=10)).astimezone(PUBLISHED.tzinfo).isoformat()
    )
    assert us.history.status == "partial"
    assert us.history.unavailable_proposal_ids == [ids[20]]
    assert us.history.has_more is True
    assert [row.proposal_id for row in us.history.proposals] == ids[19:1:-1]
    # Old exact decisions remain readable when their cash comparison horizon passed.
    for row in us.history.proposals:
        assert row.currency == "USD"
        assert row.action == "no_action"
        assert row.comparison_horizon_et == date(2027, 5, 31)
        assert row.comparison is not None
    json_text = us.model_dump_json()
    assert all(
        secret not in json_text
        for secret in (
            "synthetic analysis author",
            "synthetic analysis thesis",
            "synthetic formal notes",
            "https://",
        )
    )
    assert us.model_dump_json().count(ids[21]) == 1
    assert app.read_bytes() == before


def test_malformed_identity_occupies_an_unnamed_slot_without_leaking(tmp_path):
    app = tmp_path / "wrong-identity.sqlite"
    initialize_database(app)
    correct = _save_us(app, "us-proposal-readable")
    private = "private malformed storage-identifier canary"
    with closing(sqlite3.connect(app, autocommit=True)) as db:
        db.execute(
            "INSERT INTO us_equity_proposal VALUES (?, ?, '{}')",
            (private, "2026-06-04T00:00:00.000000+00:00"),
        )
    _, us = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert us.status == "unavailable"
    assert us.proposal is None
    assert us.history.status == "partial"
    assert [row.proposal_id for row in us.history.proposals] == [correct.decision.proposal_id]
    assert not us.history.unavailable_proposal_ids
    assert us.history.has_more is False
    assert private not in us.model_dump_json()


def test_us_history_index_failure_does_not_guess_empty_or_discard_verified_latest(tmp_path, mocker):
    app = tmp_path / "failed-index.sqlite"
    initialize_database(app)
    saved = _save_us(app, "us-proposal-known-latest")
    mocker.patch.object(
        us_source,
        "published_us_proposal_ids",
        side_effect=sqlite3.OperationalError("private canary"),
    )
    _, us = proposal_source.read_serving_proposal_views(app, generated_at=STAMP)
    assert us.status == "ready"
    assert us.proposal is not None
    assert us.proposal.proposal_id == saved.decision.proposal_id
    assert us.history.status == "unavailable"
    assert not (us.history.proposals)
    assert not (us.history.unavailable_proposal_ids)
    assert us.history.has_more is None
    assert "private canary" not in us.model_dump_json()


def test_us_history_owner_rejects_duplicate_publication_identity():
    # Deliberately damaged schema with no PK. Do not serve an ambiguous exact ID.
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE us_equity_proposal(proposal_id TEXT, published_at TEXT)")
        db.executemany(
            "INSERT INTO us_equity_proposal VALUES (?, ?)",
            [("us-proposal-duplicate", at) for at in ("2026-06-01", "2026-06-02")],
        )
        with pytest.raises(ValueError, match="duplicate US publication ID"):
            published_us_proposal_ids(db, limit=21)
