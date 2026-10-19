"""Small synthetic structural fixtures; no provider page is committed."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.batch.test_external_assessment_daily import FakeObjectStore, plan, stage
from tests.batch.test_external_assessment_raw import Session
from tests.batch.test_external_assessment_raw import connect as connect
from tests.engine.test_external_assessment_daily_verification import ASOF, seed_database
from tests.helpers.master import insert_master_rows

from baibai_batch.jobs import external_assessment_daily as external_assessment_daily
from baibai_batch.jobs.external_assessment_cohorts import make_plan
from baibai_batch.storage.external_assessment_raw import RAW_PREFIX, read_raw_bundle
from baibai_batch.storage.external_assessment_staging import read_stages
from baibai_engine.external_assessment.acquisition import collection
from baibai_engine.external_assessment.acquisition.universe import frozen_universe
from baibai_engine.external_assessment.acquisition.verification import verify_observations
from baibai_engine.external_assessment.normalization import ParseError, kabumap
from baibai_engine.external_assessment.observations.models import QuantsScoreObservation
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.store import ObservationConflictError, save_observations

RECEIVED = datetime(2026, 10, 12, 15, 0, 0, 123456, tzinfo=UTC)


def html(ticker="130A", *, cheap="1/10"):
    rows = "".join(
        f'<li class="scoreBase"><span class="scoreName">{label}</span>'
        f'<span class="scoreFigure">{cheap if label == "割安性" else "3/10"}</span></li>'
        for label in kabumap.LABELS
    )
    return (
        '<meta charset="SHIFT_JIS">'
        f'<span id="stockCode">(東証グロース : {ticker})</span>'
        "<h2>クォンツスコア情報 (2026-10-09更新)</h2>"
        f'<div id="scoreTable"><ul>{rows}</ul>'
        '<ul><li id="totalRow"><strong>42</strong>/100</li>'
        '<li id="sectorRow"><strong>52</strong>/100</li></ul></div>'
        "<h2>ファクター情報</h2><table><tr><td>PER</td><td>NA</td></tr></table>"
    )


def observation(ticker="130A", *, text=None, received=RECEIVED):
    return kabumap.parse_html(
        html(ticker) if text is None else text,
        ticker=ticker,
        source_url=kabumap.public_url(ticker),
        fetched_at_utc=received,
    )


@pytest.mark.parametrize("ticker", ["7203", "130A", "8306"])
def test_identity_na_factors_scales_and_explicit_date_are_separate_from_receipt(ticker):
    result = observation(ticker)
    assert isinstance(result, QuantsScoreObservation)
    assert result.origin == "Quants Research"
    assert result.distributor == "kabumap"
    assert result.undervaluation_score == 1
    assert result.score_scale_max == 10
    assert result.composite_score == 42
    assert result.industry_average_score == 52
    assert result.composite_scale_max == 100
    assert result.score_as_of_date == date(2026, 10, 9)
    assert result.fetched_at_utc == RECEIVED
    assert result.snapshot_date == date(2026, 10, 13)
    assert "rating" not in type(result).model_fields


@pytest.mark.parametrize(
    ("ticker", "market"),
    [("8301", ""), ("130A", "プライム"), ("130A", "スタンダード"), ("130A", "グロース")],
)
def test_tse_identity_accepts_an_omitted_market_segment(ticker, market):
    # 8301のprivate receiptは(東証:8301)。表示構造だけを合成fixtureで再現する。
    text = html(ticker).replace(f"(東証グロース : {ticker})", f"(東証{market}:{ticker})")
    result = observation(ticker, text=text)
    assert result.ticker == ticker
    assert result.parser_version == "kabumap-quants-2"
    assert result.fetched_at_utc == RECEIVED


@pytest.mark.parametrize("identity", ["(東証:8306)", "(東証プライム:8306)", "(名証:8301)"])
def test_omitted_market_segment_does_not_relax_ticker_or_venue_identity(identity):
    text = html("8301").replace("(東証グロース : 8301)", identity)
    with pytest.raises(ParseError, match="ticker_identity"):
        observation("8301", text=text)


def test_omitted_market_segment_does_not_accept_ambiguous_identity():
    text = html("8301").replace("(東証グロース : 8301)", "(東証:8301)")
    with pytest.raises(ParseError, match="ticker_identity"):
        observation("8301", text=text + '<span id="stockCode">(東証:8301)</span>')


@pytest.mark.parametrize("bad", ["11/10", "-1/10", "5/5", "NaN/10", "1.5/10"])
def test_changed_numeric_score_is_not_normal_null(bad):
    with pytest.raises(ParseError, match="changed_quants_score"):
        observation(text=html(cheap=bad))


@pytest.mark.parametrize(
    "replacement",
    [
        ("130A)", "8306)"),
        ("2026-10-09更新", "2026-99-09更新"),
        ("更新", ""),
        ("scoreTable", "renamedTable"),
        ("割安性", "変更された項目"),
        ("sectorRow", "missingSector"),
    ],
)
def test_missing_identity_date_or_score_structure_is_rejected(replacement):
    with pytest.raises(ParseError):
        observation(text=html().replace(*replacement))


def test_wrong_source_is_rejected():
    with pytest.raises(ParseError, match="source_identity"):
        kabumap.parse_html(
            html(), ticker="130A", source_url=kabumap.public_url("8306"), fetched_at_utc=RECEIVED
        )


def test_cutoff_immutable_replay_and_conflict(tmp_path):
    path = tmp_path / "market.sqlite"
    first = observation()
    later = observation(text=html(cheap="9/10"), received=RECEIVED + timedelta(days=1))
    assert save_observations(path, [first, later]) == 2
    assert save_observations(path, [first, later]) == 0
    assert read_observations(path, "130A", RECEIVED).quants_scores[0].latest == first
    history = read_observations(path, "130A", later.fetched_at_utc).quants_scores[0]
    assert (history.latest, history.previous) == (later, first)
    verify_observations(path, [first, later])
    with pytest.raises(ObservationConflictError):
        save_observations(path, [first.model_copy(update={"undervaluation_score": 4})])


def test_shift_jis_daily_checkpoint_and_single_root_raw_reparse(connect):
    session = Session([(200, html().encode("shift_jis"), "SHIFT_JIS")])
    connect(session)
    p, store = plan(tickers=("130A",), sources=("kabumap",)), FakeObjectStore()
    report = external_assessment_daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert report["provider_requests"] == 1
    assert saved.result.entries[0].state == "success"
    assert isinstance(saved.result.observations[0], QuantsScoreObservation)
    assert read_stages(store, p)[0][0] == saved
    manifest, bodies = read_raw_bundle(store, saved.raw_bundle_keys[0])
    meta, body = manifest["receipts"][0], bodies[0]
    assert body.source == "kabumap"
    assert body.role == "page"
    assert body.attempt_id == saved.result.entries[0].attempt_id
    assert body.body == html().encode("shift_jis")
    reparsed = observation(
        text=body.body.decode(meta["selected_encoding"]),
        received=datetime.fromisoformat(meta["received_at_utc"]),
    )
    assert reparsed == saved.result.observations[0]
    external_assessment_daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_daily_denial_or_rate_limit_stops_before_next_ticker(connect, monkeypatch, status):
    connect(Session([(status, b"synthetic denial", "utf-8")]))
    monkeypatch.setattr(external_assessment_daily, "_wait_for_retry", lambda *args, **kwargs: False)
    p, store = plan(tickers=("130A", "8306"), sources=("kabumap",)), FakeObjectStore()
    external_assessment_daily.acquire_cohort(store, p, 0)
    entries = stage(store, p).result.entries
    assert [entry.state for entry in entries] == ["failure", "unattempted"]
    assert entries[0].http_status == status
    assert not any(key.startswith(RAW_PREFIX) for key in store.objects)


def test_daily_plan_defaults_to_three_sources_and_accepts_an_explicit_subset():
    p = make_plan(
        run_id="synthetic",
        tickers=("130A", "8306"),
        universe_binding="fixed",
        collector_binding=collection.collector_binding(),
    )
    assert p.sources == ("monex", "rakuten", "kabumap")
    assert len(p.cohorts[0].pairs) == 6
    assert plan(sources=("monex", "rakuten")).sources == ("monex", "rakuten")


def test_bank_is_in_saved_ordinary_universe_without_financial_eligibility_filter(tmp_path):
    path = tmp_path / "market.sqlite"
    seed_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_master_rows(
            connection, [(ASOF.isoformat(), "8306", "合成銀行", "PRIME", "銀行業", "011")]
        )
    tickers, _ = frozen_universe(path, ASOF)
    assert "8306" in tickers
