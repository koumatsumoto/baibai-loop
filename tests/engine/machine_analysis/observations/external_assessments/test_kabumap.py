"""Small synthetic structural fixtures; no provider page is committed."""

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.external_assessment_html import kabumap_html as html
from tests.helpers.external_assessment_html import kabumap_not_computed_html
from tests.helpers.external_assessment_http import RawSession as Session
from tests.helpers.external_assessment_http import connect as connect
from tests.helpers.external_assessment_responses import response
from tests.helpers.external_assessment_stages import FakeObjectStore, plan, stage
from tests.helpers.external_assessment_universe import ASOF

import baibai_jobs.acquisition.external_assessments.collection as collection
import baibai_jobs.acquisition.external_assessments.daily as external_assessment_daily
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
    kabumap,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import (
    ObservationConflictError,
    save_observations,
)
from baibai_jobs.acquisition.external_assessments.cohorts import make_plan
from baibai_jobs.acquisition.external_assessments.raw import RAW_PREFIX, read_raw_bundle
from baibai_jobs.acquisition.external_assessments.staging import read_stages
from baibai_jobs.acquisition.external_assessments.universe import received_master_universe
from baibai_jobs.acquisition.master_dependencies import (
    fix_master_dependency,
    saved_master_dependency,
)

RECEIVED = datetime(2026, 10, 12, 15, 0, 0, 123456, tzinfo=UTC)


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
    assert result.availability == "available"
    assert result.origin == "Quants Research"
    assert result.distributor == "kabumap"
    assert (
        result.growth_score,
        result.undervaluation_score,
        result.size_score,
        result.technical_score,
        result.financial_health_score,
        result.market_trend_score,
    ) == (2, 1, 3, 4, 5, 6)
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
    assert result.parser_version == "kabumap-quants-4"
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


@pytest.mark.parametrize(
    "bad",
    [
        "11/10",
        "-1/10",
        "5/5",
        "NaN/10",
        "1.5/10",
        "1 0/10",
        "1/1 0",
        "1<br>0/10",
        "<strong>1 </strong><span>0</span>/10",
    ],
)
def test_changed_numeric_score_is_not_normal_null(bad):
    with pytest.raises(ParseError, match="changed_quants_score"):
        observation(text=html(cheap=bad))


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("<strong>0</strong>", 0),
        ("<strong>1</strong><span>0</span>", 10),
        ("<strong>NA</strong>", None),
        ("<strong>--</strong>", None),
    ],
)
def test_score_markup_and_outer_whitespace_preserve_tokens(cell, expected):
    text = html(cheap=f"<br> \n{cell} <br>/\n 10\t <br>")
    text = text.replace("<strong>42</strong>/100", " \n<strong>42</strong> /\n 100\t ")
    result = observation(text=text)
    assert result.undervaluation_score == expected
    assert result.composite_score == 42


@pytest.mark.parametrize("score", ["4 2/100", "42/1 00", "4<br>2/100"])
def test_composite_score_does_not_join_numeric_tokens(score):
    text = html().replace("<strong>42</strong>/100", score)
    with pytest.raises(ParseError, match="changed_quants_score"):
        observation(text=text)


@pytest.mark.parametrize(
    ("replacement", "missing", "reason"),
    [
        (("130A)", "8306)"), False, "ticker_identity"),
        (("2026-10-09更新", "2026-99-09更新"), False, "missing_quants_date"),
        (("更新", ""), False, "missing_quants_date"),
        (("2026-10-09更新", ""), True, "missing_quants_date"),
        (("scoreTable", "renamedTable"), False, "missing_quants_scores"),
        (("割安性", "変更された項目"), False, "changed_quants_score"),
        (("sectorRow", "missingSector"), False, "missing_quants_scores"),
    ],
)
def test_missing_identity_date_or_score_structure_is_rejected(replacement, missing, reason):
    with pytest.raises(ParseError, match=f"^{reason}$"):
        observation(text=html(missing=missing).replace(*replacement))


def test_explicit_not_computed_keeps_receipt_without_inventing_provider_date():
    result = observation("660A", text=kabumap_not_computed_html())
    assert result.availability == "not_computed"
    assert result.score_as_of_date is None
    assert result.fetched_at_utc == RECEIVED
    assert result.snapshot_date == date(2026, 10, 13)
    assert result.parser_version == "kabumap-quants-4"
    assert all(
        getattr(result, field) is None
        for field in (*kabumap.LABELS.values(), "composite_score", "industry_average_score")
    )


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("(東証グロース : 660A)", "(東証グロース : 130A)", "ticker_identity"),
        ("(東証グロース : 660A)", "(名証 : 660A)", "ticker_identity"),
        ("（まだ計算されていない銘柄です）", "", "missing_quants_date"),
        ("（まだ計算されていない銘柄です）", "（対象外）", "missing_quants_date"),
        ("（まだ計算されていない銘柄です）", "(2026-10-09更新)", "missing_quants_date"),
        ("クォンツスコアはまだ計算されていません", "", "missing_quants_date"),
        ("クォンツスコアはまだ計算されていません", "表示できません", "missing_quants_date"),
        ("--/10", "0/10", "changed_quants_score"),
        ("--/10", "不明/10", "changed_quants_score"),
        ("--</strong>/100", "0</strong>/100", "changed_quants_score"),
        ("割安性", "未対応項目", "changed_quants_score"),
        ("割安性", "成長性", "changed_quants_score"),
        ("scoreTable", "missingTable", "missing_quants_scores"),
        ("sectorRow", "missingSector", "missing_quants_scores"),
    ],
)
def test_not_computed_requires_complete_consistent_explicit_evidence(old, new, reason):
    with pytest.raises(ParseError, match=f"^{reason}$"):
        observation("660A", text=kabumap_not_computed_html().replace(old, new))


@pytest.mark.parametrize("tag", ["script", "style", "template", "head"])
def test_non_body_message_does_not_establish_not_computed(tag):
    text = kabumap_not_computed_html().replace(
        "<p>クォンツスコアはまだ計算されていません</p>",
        f"<{tag}>クォンツスコアはまだ計算されていません</{tag}>",
    )
    with pytest.raises(ParseError, match=r"^missing_quants_date$"):
        observation("660A", text=text)


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("<h2>クォンツスコア情報 (2026-10-09更新)</h2>", "missing_quants_date"),
        ("<h2>クォンツスコア情報 （まだ計算されていない銘柄です）</h2>", "missing_quants_date"),
        ("<h2>クォンツスコア情報 （不明）</h2>", "missing_quants_date"),
        ('<div id="scoreTable"></div>', "missing_quants_scores"),
        ('<li id="totalRow">--/100</li>', "missing_quants_scores"),
        ('<li id="sectorRow">--/100</li>', "missing_quants_scores"),
        ('<span id="stockCode">(東証:660A)</span>', "ticker_identity"),
    ],
)
def test_not_computed_rejects_duplicate_or_conflicting_structure(extra, reason):
    with pytest.raises(ParseError, match=f"^{reason}$"):
        observation("660A", text=kabumap_not_computed_html() + extra)


def test_not_computed_requires_all_six_individual_scores():
    text = kabumap_not_computed_html().replace(
        '<li class="scoreBase"><span class="scoreName">割安性</span>'
        '<span class="scoreFigure">--/10</span></li>',
        "",
    )
    with pytest.raises(ParseError, match=r"^missing_quants_scores$"):
        observation("660A", text=text)


def test_dated_all_null_remains_available_and_unknown_undated_page_fails():
    assert observation(text=html(missing=True)).availability == "available"
    with pytest.raises(ParseError, match=r"^missing_quants_date$"):
        observation(text=html(missing=True).replace(" (2026-10-09更新)", ""))


def test_not_computed_requires_the_requested_source_url():
    with pytest.raises(ParseError, match=r"^source_identity$"):
        kabumap.parse_html(
            kabumap_not_computed_html(),
            ticker="660A",
            source_url=kabumap.public_url("130A"),
            fetched_at_utc=RECEIVED,
        )


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
    with pytest.raises(ObservationConflictError):
        save_observations(path, [first.model_copy(update={"undervaluation_score": 4})])


@pytest.mark.parametrize("availability", ["available", "all_null", "not_computed"])
def test_shift_jis_daily_checkpoint_and_single_root_raw_reparse(connect, availability):
    missing = availability != "available"
    not_computed = availability == "not_computed"
    payload = (kabumap_not_computed_html("130A") if not_computed else html(missing=missing)).encode(
        "shift_jis"
    )
    session = Session([(200, payload, "SHIFT_JIS")])
    connect(session)
    p, store = plan(tickers=("130A",), sources=("kabumap",)), FakeObjectStore()
    report = external_assessment_daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert report["provider_requests"] == 1
    entry = saved.result.entries[0]
    assert entry.state == ("normal_null" if missing else "success")
    assert entry.reason == ("all_values_missing" if missing else None)
    assert entry.http_status == 200
    parsed = saved.result.observations[0]
    assert isinstance(parsed, QuantsScoreObservation)
    assert parsed.availability == ("not_computed" if not_computed else "available")
    assert parsed.score_as_of_date == (None if not_computed else date(2026, 10, 9))
    assert (
        parsed.growth_score,
        parsed.undervaluation_score,
        parsed.size_score,
        parsed.technical_score,
        parsed.financial_health_score,
        parsed.market_trend_score,
        parsed.composite_score,
        parsed.industry_average_score,
    ) == ((None,) * 8 if missing else (2, 1, 3, 4, 5, 6, 42, 52))
    assert read_stages(store, p)[0][0] == saved
    manifest, bodies = read_raw_bundle(store, saved.raw_bundle_keys[0])
    meta, body = manifest["receipts"][0], bodies[0]
    assert body.source == "kabumap"
    assert body.role == "page"
    assert body.attempt_id == saved.result.entries[0].attempt_id
    assert body.body == payload
    assert body.selected_encoding == meta["selected_encoding"] == "SHIFT_JIS"
    assert parsed.fetched_at_utc == datetime.fromisoformat(meta["received_at_utc"])
    assert decode_attempt(bodies) == parsed
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


def test_daily_plan_defaults_to_four_sources_and_accepts_an_explicit_subset():
    p = make_plan(
        run_id="synthetic",
        tickers=("130A", "8306"),
        universe_binding="fixed",
        collector_binding=collection.collector_binding(),
    )
    assert p.sources == ("monex", "rakuten", "kabumap", "minkabu")
    assert len(p.cohorts[0].pairs) == 8
    assert plan(sources=("monex", "rakuten")).sources == ("monex", "rakuten")


def test_bank_is_in_saved_ordinary_universe_without_financial_eligibility_filter(
    tmp_path, monkeypatch
):
    from tests.helpers.external_assessment_universe import build_native_master
    from tests.jobs.integration.test_master_native_models import attempt, records

    store, owner, _ = build_native_master(tmp_path, monkeypatch)
    rows = records(ASOF)
    for row in rows:
        row["MktNm"] = "PRIME"
    rows.append({**rows[0], "Code": "83060", "S33Nm": "銀行業"})
    owner.commit([attempt("bank", day=ASOF, at=RECEIVED + timedelta(minutes=1), rows=rows)])
    saved = fix_master_dependency(store, directory=tmp_path / "fixed", day=ASOF)
    tickers = received_master_universe(saved_master_dependency(store, saved, day=ASOF), ASOF)
    assert "8306" in tickers


@pytest.mark.parametrize("missing", [False, True])
def test_retained_v3_replay_preserves_recorded_version_and_its_heading_contract(missing):
    text = html(missing=missing)
    # v3 ignored additional undated score headings/body markers, but required a date.
    text += "<h2>クォンツスコア情報 （不明）</h2><p>クォンツスコアはまだ計算されていません</p>"
    raw = response(
        "kabumap", "130A", text.encode(), url=kabumap.public_url("130A"), received_at_utc=RECEIVED
    )
    old = decode_attempt([raw], expected_parser_version="kabumap-quants-3")
    assert old.parser_version == "kabumap-quants-3"
    assert old.availability == "available"
    assert old.score_as_of_date == date(2026, 10, 9)
    assert old.undervaluation_score == (None if missing else 1)
    assert old.fetched_at_utc == RECEIVED
    with pytest.raises(ParseError, match="missing_quants_date"):
        decode_attempt([raw])


def test_retained_v3_replay_cannot_reinterpret_an_undated_not_computed_page():
    raw = response(
        "kabumap",
        "660A",
        kabumap_not_computed_html().encode(),
        url=kabumap.public_url("660A"),
        received_at_utc=RECEIVED,
    )
    assert decode_attempt([raw]).availability == "not_computed"
    with pytest.raises(ParseError, match="missing_quants_date"):
        decode_attempt([raw], expected_parser_version="kabumap-quants-3")
    with pytest.raises(ParseError, match="unsupported_parser_version"):
        decode_attempt([raw], expected_parser_version="kabumap-quants-unknown")


def test_not_computed_requires_composite_scores_inside_the_complete_table():
    text = (
        kabumap_not_computed_html().replace('<li id="sectorRow"><strong>--</strong>/100</li>', "")
        + '<li id="sectorRow"><strong>--</strong>/100</li>'
    )
    with pytest.raises(ParseError, match="missing_quants_scores"):
        observation("660A", text=text)
