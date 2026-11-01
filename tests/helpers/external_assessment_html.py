"""Synthetic public HTML shared by parser, acquisition, replay and publication tests."""

from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    minkabu,
    rakuten,
)


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


def rakuten_no_estimates_html():
    # Synthetic form of the shared live shell. Asset names and cache keys are
    # not response identity; 6091 also occurs incidentally inside 20260918.
    return """<html><head><title>合成ページ</title>
    <link rel="stylesheet" href="css/system/default.css?20260918">
    <script src="js/chart_op_cons.min.js"></script></head><body>
    <body id="system"><div id="str-container"><div id="str-main">
    <div class="box-02"><p>この銘柄のアナリスト業績予測情報はありません。</p></div>
    </div></div></body></body></html>"""


def kabumap_html(ticker="130A", *, cheap="1/10"):
    rows = "".join(
        f'<li class="scoreBase"><span class="scoreName">{label}</span>'
        f'<span class="scoreFigure">{score}</span></li>'
        for label, score in (
            ("市場トレンド", "6/10"),
            ("割安性", cheap),
            ("企業規模", "3/10"),
            ("成長性", "2/10"),
            ("財務健全性", "5/10"),
            ("テクニカル", "4/10"),
        )
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


def minkabu_html(ticker="7203", *, eps="4.5", with_forecast=True):
    forecast = """
    <div class="ly_content_wrapper"><h3>2027年の業績予想</h3>
    <table><tr><th rowspan="2"></th>
    <th colspan="4">証券アナリスト予想</th><th>会社予想</th></tr>
    <tr><th>3ヶ月前(2026/07/24)</th><th>1ヶ月前(2026/09/22)</th>
    <th>1週間前(2026/10/15)</th><th>最新(2026/10/22)</th><th>最新(2026/09/17)</th></tr>
    <tr><th>売上高</th><td>90</td><td>95</td><td>100</td><td>123</td><td>120</td></tr>
    <tr><th>当期利益</th><td>1</td><td>2</td><td>3</td><td>-4</td><td>5</td></tr>
    <tr><th>1株当り利益</th><td>1</td><td>2</td><td>3</td><td>EPS</td><td>6</td></tr>
    </table>※金額の単位：百万円（1株当りの項目のみ：円）</div>
    <div id="v-analyst-consensus"><table><tr><th>2027年3月期<br>アナリスト予想</th>
    <th>2027年3月期<br>会社予想</th></tr></table>
    <performance-trends-chart data-value="9999"></performance-trends-chart></div>
    """.replace("EPS", eps)
    return f"""
    <html><head><title>架空会社 ({ticker}) : アナリスト予想</title>
    <link rel="canonical" href="{minkabu.public_url(ticker)}"></head><body>
    <div><h2>目標株価</h2><span>99999円</span>総合予想・個人予想</div>
    <div class="md_card_ti theme_buy">
    <h2>アナリスト予想<br><span>(2026/10/22)</span></h2>
    <a class="md_picksPlate">買い</a><span class="fsxxl">100</span></div>
    <table><tr><th>強気買い</th><th>買い</th><th>中立</th><th>売り</th><th>強気売り</th></tr>
    <tr><td></td><td></td><td></td><td></td><td></td></tr>
    <tr><td>2人</td><td>1人</td><td>3人</td><td>0人</td><td>0人</td></tr></table>
    <table><tr><th></th><th>3ヶ月前</th><th>1ヶ月前</th><th>1週間前</th><th>最新</th></tr>
    <tr><th>評価</th><td>中立</td><td>中立</td><td>買い</td><td>買い</td></tr>
    <tr><th>予想株価</th><td>80</td><td>90</td><td>95</td><td>100</td></tr></table>
    「アナリストの予想株価」は、予想時点から1年後の株価を予想しています。
    {forecast if with_forecast else ""}
    </body></html>
    """


def no_coverage_html(ticker="130A", *, linked=False):
    tag = "a" if linked else "span"
    href = f' href="/stock/{ticker}/analyst_consensus"' if linked else ""
    return f'''<html><head><title>合成会社 ({ticker}) : 株価</title>
    <link rel="canonical" href="{minkabu.profile_url(ticker)}"></head><body>
    <span class="md_box"><p class="label">個人予想</p><span>対象外</span></span>
    <{tag} class="ly_col md_box"{href}><p class="label">アナリスト</p>
    <span class="md_picksPlate theme_none">対象外</span></{tag}></body></html>'''
