from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from baibai_engine.market.retail_dynamics.sources import ParseError, monex, rakuten

RECEIVED = datetime(2026, 10, 10, 16, 30, tzinfo=UTC)


def monex_html(ticker="7203", *, value="1,234円"):
    tables = "".join(
        f"<table><tr><th>{label}</th></tr><tr><td>{value}</td></tr></table>"
        for label in (
            "PER基準<span>会社予想</span>",
            "PER基準<span>アナリスト予想</span>",
            "PBR基準",
            "目標株価コンセンサス",
        )
    )
    return (
        f'<input id="stockCd" value="{ticker}"><div class="theory_summary_inner">'
        "<table><tr><th>株価 (10/10)</th></tr><tr><td>999円</td></tr></table>"
        + tables
        + '</div><span class="cheap">割安</span><span class="expensive">割高</span>'
        + '<div><th>レーティング</th><img class="summary_blur" src="locked.png"></div>'
    )


def official_html(ticker="7203"):
    return f'''<meta property="og:url" content="{rakuten.public_url(ticker)}">
    <script>var requestURL="{rakuten.IFRAME_BASE}";
    var requestPARAM="{rakuten.IFRAME_PARAMETER}";
    ifrSrc = requestURL + requestPARAM + getRIC();</script><iframe id="ifr" src="/blank.html"></iframe>'''


def rakuten_html(ticker="7203", *, value="3.25"):
    return f"""<div class="segment-s last-child"><h3><span>レーティング</span></h3>
    <table><thead><tr><th>現在値</th><th>1週間前</th><th>1ヶ月前</th><th>3ヶ月前</th></tr></thead>
    <tbody><tr><td><em>{value}</em></td><td>4.10</td><td>4.20</td><td>4.30</td></tr></tbody></table></div>
    <div><h3>コンセンサスの推移（数値）</h3><span>アナリスト数：99人</span></div>
    <table><thead><tr><th>コード</th><th>会社名</th><th>レーティング</th></tr></thead>
    <tbody><tr><th>{ticker}</th><td>合成会社</td><td>{value}</td></tr><tr><th>9432</th><td>合成別会社</td><td>2.0</td></tr></tbody></table>"""


def rakuten_chart_binding(ticker="7203"):
    return f'''<div id="chart_op_cons"></div><script>
    new ChartOpConsClass("#chart_op_cons", "js/jsonp/chart_op_cons.json",
        "https://img-sec.ifis.co.jp/graph/chart_op_cons/{ticker}.json", "{ticker}");
    </script>'''


def rakuten_peer_only_html(ticker="7203"):
    return rakuten_html("9432", value="--") + rakuten_chart_binding(ticker)


def parse_monex(html=None, **kwargs):
    return monex.parse_html(
        html or monex_html(),
        ticker="7203",
        source_url=monex.public_url("7203"),
        fetched_at_utc=RECEIVED,
        **kwargs,
    )


def parse_rakuten(html=None):
    return rakuten.parse_html(
        html or rakuten_html(),
        ticker="7203",
        source_url=rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "7203.T",
        fetched_at_utc=RECEIVED,
    )


def test_monex_four_model_values_and_receipt_only():
    observation = parse_monex()
    assert [
        observation.consensus_target,
        observation.per_company_forecast,
        observation.per_consensus,
        observation.pbr,
    ] == [1234] * 4
    assert observation.unit == "JPY/share"
    assert observation.currency == "JPY"
    assert observation.fetched_at_utc == RECEIVED
    assert observation.snapshot_date.isoformat() == "2026-10-11"
    assert observation.consensus_target_count is None
    assert observation.per_consensus_period is None
    assert observation.per_consensus_updated_at_utc is None
    assert observation.share_basis is None
    assert "割安" not in observation.model_dump_json()


def test_monex_verified_null_is_observation():
    observation = parse_monex(monex_html(value="--"))
    assert observation.consensus_target is None
    assert observation.per_company_forecast is None


@pytest.mark.parametrize("ticker", ["1301", "130A"])
def test_monex_fullwidth_missing_models_preserve_partial_values_and_identity(ticker):
    html = monex_html(ticker).replace("1,234円", "－", 2)
    observation = monex.parse_html(
        html, ticker=ticker, source_url=monex.public_url(ticker), fetched_at_utc=RECEIVED
    )
    assert observation.per_company_forecast is None
    assert observation.per_consensus is None
    assert observation.pbr == 1234
    assert observation.consensus_target == 1234
    assert observation.fetched_at_utc == RECEIVED
    with pytest.raises(ParseError, match="ticker_identity"):
        monex.parse_html(
            html, ticker="1333", source_url=monex.public_url("1333"), fetched_at_utc=RECEIVED
        )


def test_monex_fullwidth_missing_all_models_is_verified_normal_null():
    observation = parse_monex(monex_html(value="－"))
    assert all(
        getattr(observation, metric) is None
        for metric in ("consensus_target", "per_company_forecast", "per_consensus", "pbr")
    )


def test_monex_unknown_numeric_marker_stays_parse_failure():
    with pytest.raises(ParseError, match="unexpected_numeric_cell"):
        parse_monex(monex_html(value="未確認"))


@pytest.mark.parametrize(
    "html",
    [
        "<html>login</html>",
        monex_html("9432"),
        monex_html().replace("PBR基準", "別モデル"),
        monex_html().replace("1,234円", "1,234米ドル"),
        monex_html().replace("1,234円", '<img src="locked.png">'),
        monex_html().replace("1,234円", ""),
    ],
)
def test_monex_drift_identity_units_and_locks_fail(html):
    with pytest.raises(ParseError):
        parse_monex(html)


def test_monex_rejects_naive_receipt_and_source_identity():
    with pytest.raises(ValidationError):
        monex.parse_html(
            monex_html(),
            ticker="7203",
            source_url=monex.public_url("7203"),
            fetched_at_utc=RECEIVED.replace(tzinfo=None),
        )
    with pytest.raises(ParseError):
        monex.parse_html(
            monex_html(),
            ticker="7203",
            source_url=monex.public_url("9432"),
            fetched_at_utc=RECEIVED,
        )


def test_rakuten_follows_official_full_ric():
    assert (
        rakuten.iframe_url(official_html(), ticker="7203", source_url=rakuten.public_url("7203"))
        == rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "7203.T"
    )


@pytest.mark.parametrize(
    "html",
    [
        official_html("9432"),
        official_html().replace("rakuten-sec.ifis.co.jp", "private.example"),
        official_html().replace("report_est", "private_est"),
        official_html().replace("getRIC()", "guessTicker()"),
        "<html>shell</html>",
    ],
)
def test_rakuten_refuses_changed_official_route(html):
    with pytest.raises(ParseError):
        rakuten.iframe_url(html, ticker="7203", source_url=rakuten.public_url("7203"))


def test_rakuten_current_rating_without_forecast_count_or_past_observations():
    observation = parse_rakuten()
    assert observation.rating == 3.25
    assert observation.rating_count is None
    assert observation.rating_scale_min is None
    assert observation.rating_scale_max is None
    assert observation.rating_scale_direction is None
    assert observation.fetched_at_utc == RECEIVED
    assert observation.rating_updated_at_utc is None
    assert "4.1" not in observation.model_dump_json()


def test_rakuten_null_and_zero_are_distinct():
    assert parse_rakuten(rakuten_html(value="--")).rating is None
    assert parse_rakuten(rakuten_html(value="0")).rating == 0


@pytest.mark.parametrize("ticker", ["1301", "130A"])
def test_rakuten_no_forecast_shell_cannot_prove_ticker_or_null_rating(ticker):
    html = '<div class="box-01"><p>この銘柄のアナリスト業績予測情報はありません。</p></div>'
    with pytest.raises(ParseError, match=r"^no_estimates_identity_unconfirmed$"):
        rakuten.parse_html(
            html,
            ticker=ticker,
            source_url=rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + ticker + ".T",
            fetched_at_utc=RECEIVED,
        )


def test_rakuten_generic_empty_shell_is_not_no_forecast_classification():
    with pytest.raises(ParseError, match=r"^missing_ticker_identity$"):
        parse_rakuten("<html><p>情報を表示できません。</p></html>")


@pytest.mark.parametrize("ticker", ["1379", "135A"])
def test_rakuten_peer_only_table_with_bound_chart_proves_normal_null(ticker):
    url = rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + ticker + ".T"
    observation = rakuten.parse_html(
        rakuten_peer_only_html(ticker), ticker=ticker, source_url=url, fetched_at_utc=RECEIVED
    )
    assert observation.rating is None
    assert observation.rating_count is None
    assert observation.source_url == url
    assert observation.fetched_at_utc == RECEIVED
    assert observation.parser_version == "rakuten-rating-3"


@pytest.mark.parametrize(
    "html",
    [
        rakuten_peer_only_html().replace("7203", "6758"),
        rakuten_peer_only_html().replace("chart_op_cons/7203.json", "chart_op_cons/6758.json"),
        rakuten_peer_only_html() + rakuten_chart_binding(),
        rakuten_peer_only_html() + rakuten_chart_binding("6758"),
        rakuten_peer_only_html().replace('"7203");', "getTicker());"),
        rakuten_peer_only_html().replace('"7203");', '"7203", "extra");'),
        rakuten_peer_only_html().replace('<div id="chart_op_cons"></div>', ""),
        rakuten_peer_only_html() + '<div id="chart_op_cons"></div>',
        rakuten_peer_only_html().replace('id="chart_op_cons"', 'id="other_widget"'),
        rakuten_peer_only_html().replace("js/jsonp/chart_op_cons.json", "js/unknown.json"),
        rakuten_peer_only_html().replace("img-sec.ifis.co.jp", "other.example"),
        rakuten_peer_only_html().replace("new ChartOpConsClass", "// new ChartOpConsClass"),
        rakuten_peer_only_html()
        .replace("new ChartOpConsClass", "/* new ChartOpConsClass")
        .replace("</script>", "*/</script>"),
        rakuten_peer_only_html()
        .replace("new ChartOpConsClass", "<!-- new ChartOpConsClass")
        .replace("</script>", "--></script>"),
        rakuten_html() + rakuten_chart_binding("6758"),
    ],
)
def test_rakuten_unverified_or_conflicting_chart_binding_fails(html):
    with pytest.raises(ParseError):
        parse_rakuten(html)


@pytest.mark.parametrize("quote", ["single", "double", "template"])
def test_rakuten_constructor_text_inside_string_is_not_identity(quote):
    binding = rakuten_chart_binding().split("<script>", 1)[1].split("</script>", 1)[0]
    literal = (
        json.dumps(binding)
        if quote == "double"
        else f"`{binding}`"
        if quote == "template"
        else "'" + binding.replace("\n", "") + "'"
    )
    html = rakuten_html("9432", value="--") + f"<script>var example = {literal};</script>"
    with pytest.raises(ParseError, match="ticker_identity"):
        parse_rakuten(html)


def test_rakuten_chart_identity_does_not_relax_rating_table_contract():
    html = rakuten_peer_only_html().replace("現在値", "別の値")
    with pytest.raises(ParseError, match="changed_rating_headers"):
        parse_rakuten(html)


@pytest.mark.parametrize(
    "html",
    [
        "<html>login</html>",
        rakuten_html("6758"),
        rakuten_html().replace("現在値", "最新値"),
        rakuten_html().replace("3.25", "unavailable"),
        rakuten_html().replace("3.25", '<img src="locked.png">'),
    ],
)
def test_rakuten_drift_and_response_identity_fail(html):
    with pytest.raises(ParseError):
        parse_rakuten(html)


@pytest.mark.parametrize("bcode", ["7203", "7203.T"])
def test_rakuten_captured_public_urls_keep_original_provenance(bcode):
    url = rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + bcode
    observation = rakuten.parse_html(
        rakuten_html(), ticker="7203", source_url=url, fetched_at_utc=RECEIVED
    )
    assert observation.source_url == url
    assert observation.fetched_at_utc == RECEIVED
    with pytest.raises(ParseError):
        rakuten.parse_html(rakuten_html(), ticker="9432", source_url=url, fetched_at_utc=RECEIVED)


def test_monex_negative_model_value_is_not_missing():
    assert parse_monex(monex_html(value="-50円")).per_company_forecast == -50


def test_monex_distinct_series_are_mapped_to_matching_labels():
    html = (
        monex_html()
        .replace("1,234円", "222円", 1)
        .replace("1,234円", "333円", 1)
        .replace("1,234円", "444円", 1)
    )
    observed = parse_monex(html)
    assert observed.consensus_target == 1234
    assert observed.per_company_forecast == 222
    assert observed.per_consensus == 333
    assert observed.pbr == 444
