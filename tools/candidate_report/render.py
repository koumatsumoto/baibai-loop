"""Render the stage-1 candidate shortlist report (self-contained HTML).

The report is the human-review gate of the opportunity path: the audit-pool
candidates required by decision-cycle OP3 are presented with first-layer facts
and selection background so the operator can pick the primary-research set.

Design mirrors packet-scaffold: the machine plumbs every hard number from the
screening output (price, valuation, dividend basis, expected return, fair-value
anchor, input hashes), and the operator supplies only the qualitative narrative
through ``narratives.yaml``. No figure is hand-transcribed into the report.

The generated HTML is an ephemeral artifact (write it under ``.cache``); it is
not committed. Re-running the screen and this renderer reproduces it.

Usage::

    uv run python -m tools.candidate_report.render \\
        --selection .cache/opportunity/ASOF/selection-output.yaml \\
        --candidates .cache/opportunity/ASOF/candidates.yaml \\
        --narratives .cache/opportunity/ASOF/narratives.yaml \\
        --prepared .cache/opportunity/ASOF/selection.yaml \\
        --out .cache/opportunity/ASOF/candidate-report.html

``--prepared`` points at the ``baibai-loop-opportunity prepare`` workspace
output (``selection.yaml``): its ``audit_pool`` rows carry
``portfolio_annotation`` (unheld/held/reserved/held_and_reserved), the one
fact ``selection-output.yaml`` does not know because screening runs
portfolio-blind.

``narratives.yaml`` schema (see ``narratives-template.yaml``)::

    meta:
      title: str
      target_session: "YYYY-MM-DD"   # optional
      order_by: str                   # optional, describes the display order
      intro_notes: [str, ...]         # optional bullet list under the brief
    candidates:                        # ordered; order == display order
      - ticker: "7095"
        sector_label: str              # optional label; defaults to sector_33
        ploss: "低|中低|中|要精査|高"   # preliminary permanent-loss read
        prov: str                       # provisional disposition
        why: str
        temporary: str
        structural: str
        survive: str
        unlock: str
        counter: str
        research: str
        value: str
    excluded:                          # audit-pool tickers not shortlisted
      - ticker: "6417"
        reason: str
"""

from __future__ import annotations

import argparse
import hashlib
import html
from datetime import date
from pathlib import Path
from typing import Any

import yaml

TRADINGVIEW = "https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A{ticker}"

# Narrative key -> display heading, in render order.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("why", "なぜ安くなっている可能性があるか"),
    ("temporary", "一時的な問題である可能性"),
    ("structural", "構造的な問題である可能性"),
    ("survive", "財務的に5年間耐えられそうか"),
    ("unlock", "何が改善すれば株主価値が上がるか"),
    ("counter", "最も強い反対仮説"),
    ("research", "個別リサーチで確認する事項"),
    ("value", "この候補を深掘りする価値"),
)
PLOSS_CLASS = {"低": "lo", "中低": "mlo", "中": "mid", "要精査": "hi", "高": "hi"}
PORTFOLIO_ANNOTATION_LABELS = {
    "unheld": "未保有",
    "held": "保有中（買増し候補）",
    "reserved": "予約中（active reservationあり）",
    "held_and_reserved": "保有中＋予約中",
}


class ReportError(ValueError):
    """A narrative refers to data the screening output does not contain."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _candidate_index(candidates_doc: Any) -> dict[str, dict[str, Any]]:
    rows = (
        candidates_doc["candidates"]
        if isinstance(candidates_doc, dict) and "candidates" in candidates_doc
        else candidates_doc
    )
    return {row["ticker"]: row for row in rows}


def esc(value: object) -> str:
    return html.escape(str(value))


def fnum(value: object, mul: float = 1.0, nd: int = 1, suf: str = "", *, sign: bool = False) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fmt = f"{{:+,.{nd}f}}" if sign else f"{{:,.{nd}f}}"
        return f"{fmt.format(value * mul)}{suf}"
    return "—"


def _fv_gap_pct(entry: dict[str, Any]) -> float | None:
    fv = entry.get("fair_value_anchor_yen")
    px = entry.get("market_price_yen")
    if isinstance(fv, (int, float)) and isinstance(px, (int, float)) and px:
        return (fv / px - 1) * 100
    return None


def _price_label(asof: object) -> str:
    if isinstance(asof, date):
        parsed = asof
    elif isinstance(asof, str):
        try:
            parsed = date.fromisoformat(asof)
        except ValueError:
            return "screening参考価格"
    else:
        return "screening参考価格"
    return f"{parsed.month}/{parsed.day} screening参考価格"


def _portfolio_annotation_label(prep: dict[str, Any]) -> str:
    raw = prep.get("portfolio_annotation")
    if raw is None:
        return "—"
    return esc(PORTFOLIO_ANNOTATION_LABELS.get(str(raw), str(raw)))


def _earnings_cell(cand: dict[str, Any], entry: dict[str, Any]) -> str:
    raw = cand.get("next_earnings_date")
    label = esc(raw) if raw else "未定/JPX未公表"
    warnings = [str(w) for w in (entry.get("event_warnings") or [])]
    if warnings:
        label = f"{label}（warning: {esc(', '.join(warnings))}）"
    return label


def _er_decomposition_cell(metrics: dict[str, Any]) -> str:
    reversion = fnum(metrics.get("er_reversion_annual"), 100, 1, "%", sign=True)
    carry = fnum(metrics.get("er_carry_annual"), 100, 1, "%", sign=True)
    return f"reversion {reversion} + carry {carry}"


def _price_position_cell(cand: dict[str, Any]) -> str:
    change_60d = fnum(cand.get("price_change_60d"), 100, 1, "%", sign=True)
    gap = fnum(cand.get("gap_from_52w_low"), 100, 1, "%", sign=True)
    return f"60日 {change_60d} ／ 52週安値から {gap}"


def _growth_yoy_cell(metrics: dict[str, Any]) -> str:
    sales = fnum(metrics.get("sales_yoy"), 100, 1, "%", sign=True)
    op = fnum(metrics.get("operating_profit_yoy"), 100, 1, "%", sign=True)
    return f"売上 {sales} ／ 営業益 {op}"


def _fv_anchor_cell(metrics: dict[str, Any]) -> str:
    self_range = fnum(metrics.get("fv_self_range_yen"), 1, 0, " 円")
    sector_median = fnum(metrics.get("fv_sector_median_yen"), 1, 0, " 円")
    text = f"自社レンジ {self_range} ／ 業種中央値 {sector_median}"
    anchor = metrics.get("er_anchor_metrics")
    if anchor:
        text += f"（anchor: {esc(anchor)}）"
    return text


def _liquidity_cell(cand: dict[str, Any], entry: dict[str, Any]) -> str:
    turnover = fnum(cand.get("avg_turnover_oku"), 1, 1, " 億円/日")
    status = entry.get("liquidity_status")
    return f"{turnover} ／ {esc(status) if status else '—'}"


def _data_quality_cell(cand: dict[str, Any], metrics: dict[str, Any]) -> str:
    warnings = [str(w) for w in (cand.get("freshness_warnings") or [])]
    edinet_failure = metrics.get("edinet_failure_reasons")
    if edinet_failure:
        warnings.append(f"edinet: {edinet_failure}")
    lag = metrics.get("bs_carry_forward_lag_days")
    if isinstance(lag, (int, float)) and not isinstance(lag, bool) and lag > 0:
        warnings.append(f"BS前期繰越 {fnum(lag, 1, 0)}日")
    ttm_quality = cand.get("ttm_quality") or {}
    non_exact = [key for key, value in ttm_quality.items() if value != "exact"]
    if non_exact:
        warnings.append(f"ttm非exact: {','.join(non_exact)}")
    return esc(", ".join(warnings)) if warnings else "なし"


def _fact_rows(
    ticker: str,
    cand: dict[str, Any],
    entry: dict[str, Any],
    prep: dict[str, Any],
    nar: dict[str, Any],
    asof: object,
) -> str:
    metrics = cand.get("metrics", {})
    px = entry.get("market_price_yen")
    fv = entry.get("fair_value_anchor_yen")
    gap = _fv_gap_pct(entry)
    net_cash = metrics.get("net_cash_to_market_cap")
    net_label = "net cash" if isinstance(net_cash, (int, float)) and net_cash >= 0 else "net debt"
    split_factor = metrics.get("dividend_split_factor")
    corp = (
        f"株式分割補正あり（split factor {fnum(split_factor, 1, 4)}）"
        if split_factor
        else "直近の分割・特別配当補正なし"
    )
    sector = nar.get("sector_label") or cand.get("sector_33") or "—"
    rows: list[tuple[str, str]] = [
        (
            "TradingView",
            f'<a href="{TRADINGVIEW.format(ticker=ticker)}" target="_blank" rel="noopener">TSE:{esc(ticker)} チャート ↗</a>',
        ),
        (_price_label(asof), fnum(px, 1, 1, " 円")),
        ("時価総額", fnum(cand.get("market_cap_oku"), 1, 0, " 億円")),
        ("業種", esc(sector)),
        ("次回決算予定", _earnings_cell(cand, entry)),
        (
            "screening順位 / E[r]",
            f"{entry.get('rank', '—')}位 / <b>{fnum(entry.get('expected_return_pct'), 1, 2, '%')}</b>",
        ),
        ("機械E[r]分解", _er_decomposition_cell(metrics)),
        (
            "FVアンカー / 乖離",
            f"{fnum(fv, 1, 0, ' 円')} / <b>{fnum(gap, 1, 0, '%', sign=True)}</b>",
        ),
        ("FVアンカー構成", _fv_anchor_cell(metrics)),
        ("値位置", _price_position_cell(cand)),
        (
            "PER(予/実) / PBR",
            f"{fnum(cand.get('per_forward'), 1, 1)} / {fnum(cand.get('per_trailing'), 1, 1)} ・ {fnum(cand.get('pbr'), 1, 2)}",
        ),
        ("EV/EBITDA", fnum(cand.get("ev_ebitda"), 1, 1)),
        ("売上/営業益 YoY", _growth_yoy_cell(metrics)),
        ("自己資本比率", fnum(metrics.get("equity_ratio"), 100, 0, "%")),
        (f"{net_label} / 時価総額", fnum(net_cash, 100, 0, "%")),
        (
            "OCF / FCF イールド",
            f"{fnum(metrics.get('ocf_yield'), 100, 1, '%')} / {fnum(metrics.get('fcf_yield'), 100, 1, '%')}",
        ),
        (
            "実績→予想 配当",
            f"{fnum(metrics.get('dps_actual_annual'), 1, 1)} → {fnum(metrics.get('dps_forecast_annual'), 1, 1)} 円（利回り {fnum(metrics.get('dividend_yield'), 100, 2, '%')}, basis={esc(metrics.get('dividend_basis'))}）",
        ),
        ("流動性", _liquidity_cell(cand, entry)),
        ("corporate action", corp),
        ("データ品質", _data_quality_cell(cand, metrics)),
        ("portfolio状態", _portfolio_annotation_label(prep)),
        ("暫定判断", f"<b>{esc(nar.get('prov', '—'))}</b>"),
    ]
    return "\n".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows)


def _card(
    idx: int,
    ticker: str,
    cand: dict[str, Any],
    entry: dict[str, Any],
    prep: dict[str, Any],
    nar: dict[str, Any],
    asof: object,
) -> str:
    facts = _fact_rows(ticker, cand, entry, prep, nar, asof)
    secs = "\n".join(
        f'<div class="sec"><h4>{esc(heading)}</h4><p>{esc(nar.get(key, "—"))}</p></div>'
        for key, heading in SECTIONS
    )
    ploss = nar.get("ploss", "—")
    ploss_cls = PLOSS_CLASS.get(str(ploss), "mid")
    name = esc(cand.get("name", ticker))
    return f"""
<section class="card" id="c{esc(ticker)}">
  <div class="card-head">
    <h3><span class="rank">{idx}</span> {esc(ticker)} {name}</h3>
    <span class="ploss {ploss_cls}">永久損失リスク（暫定）: {esc(ploss)}</span>
  </div>
  <div class="card-body">
    <table class="facts">{facts}</table>
    <div class="secs">{secs}</div>
  </div>
</section>"""


def _comparison_rows(
    order: list[str], cand_by: dict[str, Any], pool: dict[str, Any], nar_by: dict[str, Any]
) -> str:
    out = []
    for i, ticker in enumerate(order, start=1):
        cand = cand_by[ticker]
        entry = pool[ticker]
        nar = nar_by[ticker]
        metrics = cand.get("metrics", {})
        gap = _fv_gap_pct(entry)
        er_decomp = (
            f"{fnum(metrics.get('er_reversion_annual'), 100, 1, sign=True)}"
            f"/{fnum(metrics.get('er_carry_annual'), 100, 1, sign=True)}%"
        )
        next_earnings = cand.get("next_earnings_date")
        out.append(
            f"<tr><td>{i}</td>"
            f'<td><a href="{TRADINGVIEW.format(ticker=ticker)}" target="_blank" rel="noopener">{esc(ticker)}</a> {esc(cand.get("name", ""))}</td>'
            f'<td class="num">{fnum(entry.get("market_price_yen"), 1, 1)}</td>'
            f'<td class="num">PBR {fnum(cand.get("pbr"), 1, 2)} / PERf {fnum(cand.get("per_forward"), 1, 1)}</td>'
            f"<td>{esc(nar.get('ploss', '—'))}</td>"
            f'<td class="num">{fnum(metrics.get("equity_ratio"), 100, 0, "%")} / {fnum(metrics.get("net_cash_to_market_cap"), 100, 0, "%")}</td>'
            f'<td class="num">{fnum(entry.get("expected_return_pct"), 1, 2, "%")}</td>'
            f'<td class="num">{er_decomp}</td>'
            f'<td class="num">{fnum(cand.get("price_change_60d"), 100, 1, "%", sign=True)}</td>'
            f'<td class="num">{fnum(gap, 1, 0, "%", sign=True)}</td>'
            f"<td>{esc(next_earnings) if next_earnings else '—'}</td>"
            f"<td>{esc(nar.get('prov', '—'))}</td></tr>"
        )
    return "\n".join(out)


def render(*, selection: Path, candidates: Path, narratives: Path, prepared: Path) -> str:
    sel_doc = _load(selection)
    cand_by = _candidate_index(_load(candidates))
    nar_doc = _load(narratives)
    prepared_doc = _load(prepared)

    pool = {e["ticker"]: e for e in sel_doc.get("audit_pool", [])}
    prepared_pool = {e["ticker"]: e for e in prepared_doc.get("audit_pool", [])}
    meta = nar_doc.get("meta", {}) or {}
    nar_list = nar_doc.get("candidates", []) or []
    order = [c["ticker"] for c in nar_list]
    nar_by = {c["ticker"]: c for c in nar_list}

    missing = [t for t in order if t not in pool or t not in cand_by]
    if missing:
        raise ReportError(f"narrative tickers not in screening audit pool/candidates: {missing}")

    missing_prepared = [t for t in order if t not in prepared_pool]
    if missing_prepared:
        raise ReportError(
            "narrative tickers not in prepared selection (prepare output) audit pool: "
            f"{missing_prepared}"
        )

    asof = (sel_doc.get("selection", {}) or {}).get("asof", "—")
    title = meta.get("title", f"日本株 候補レポート — 第1段階（{asof} 基準）")
    order_by = meta.get("order_by", "運用者指定順")
    intro = "\n".join(f"<li>{esc(note)}</li>" for note in meta.get("intro_notes", []))
    intro_block = f"<ul>{intro}</ul>" if intro else ""

    cards = "\n".join(
        _card(i, t, cand_by[t], pool[t], prepared_pool[t], nar_by[t], asof)
        for i, t in enumerate(order, start=1)
    )
    comp = _comparison_rows(order, cand_by, pool, nar_by)
    excl = "\n".join(
        f"<li><b>{esc(x['ticker'])} {esc(cand_by.get(x['ticker'], {}).get('name', ''))}</b> — {esc(x['reason'])}</li>"
        for x in (nar_doc.get("excluded", []) or [])
    )
    n = len(order)

    return _TEMPLATE.format(
        title=esc(title),
        asof=esc(asof),
        target=esc(meta.get("target_session", "—")),
        order_by=esc(order_by),
        intro_block=intro_block,
        n=n,
        cards=cards,
        comp_rows=comp,
        excl_rows=excl,
        cand_hash=_sha256(candidates),
        sel_hash=_sha256(selection),
        prepared_hash=_sha256(prepared),
        selection_path=esc(selection),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the stage-1 candidate shortlist report.")
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--narratives", required=True, type=Path)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    doc = render(
        selection=args.selection,
        candidates=args.candidates,
        narratives=args.narratives,
        prepared=args.prepared,
    )
    args.out.write_text(doc, encoding="utf-8")
    print(f"wrote {args.out} ({len(doc)} bytes)")
    return 0


_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; script-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>{title}</title>
<style>
:root {{
  --bg:#f7f7f5; --card:#fff; --ink:#1a1a1a; --muted:#666; --line:#e3e3de;
  --accent:#2b6cb0; --lo:#2f855a; --mlo:#38a169; --mid:#b7791f; --hi:#c05621;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16181c; --card:#1e2126; --ink:#e8e8e6; --muted:#9aa0a6; --line:#2c3036;
    --accent:#63b3ed; --lo:#68d391; --mlo:#68d391; --mid:#f6ad55; --hi:#f6906a; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif;
  line-height:1.7; font-size:15px; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:28px 20px 80px; }}
h1 {{ font-size:26px; margin:0 0 6px; }}
h2 {{ font-size:20px; margin:44px 0 14px; padding-bottom:6px; border-bottom:2px solid var(--line); }}
.lede {{ color:var(--muted); font-size:14px; margin:0 0 20px; }}
.brief {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 20px; margin:18px 0; }}
.brief ul {{ margin:8px 0 0; padding-left:20px; }} .brief li {{ margin:3px 0; }}
.pill {{ display:inline-block; background:var(--accent); color:#fff; border-radius:999px; padding:2px 10px; font-size:12px; font-weight:600; margin-right:6px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; margin:22px 0; overflow:hidden; }}
.card-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:14px 18px; border-bottom:1px solid var(--line); flex-wrap:wrap; }}
.card-head h3 {{ margin:0; font-size:18px; }}
.rank {{ display:inline-grid; place-items:center; width:26px; height:26px; border-radius:50%; background:var(--accent); color:#fff; font-size:14px; margin-right:6px; vertical-align:middle; }}
.ploss {{ font-size:12px; font-weight:700; padding:3px 10px; border-radius:6px; white-space:nowrap; color:#fff; }}
.ploss.lo {{ background:var(--lo); }} .ploss.mlo {{ background:var(--mlo); }} .ploss.mid {{ background:var(--mid); }} .ploss.hi {{ background:var(--hi); }}
.card-body {{ display:grid; grid-template-columns:340px 1fr; gap:0; }}
@media (max-width:820px) {{ .card-body {{ grid-template-columns:1fr; }} }}
table.facts {{ border-collapse:collapse; width:100%; font-size:13px; border-right:1px solid var(--line); }}
table.facts th, table.facts td {{ text-align:left; padding:7px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
table.facts th {{ color:var(--muted); font-weight:600; width:42%; }}
table.facts a, table.comp a {{ color:var(--accent); text-decoration:none; }}
.secs {{ padding:6px 18px 12px; }}
.sec {{ margin:12px 0; }} .sec h4 {{ margin:0 0 3px; font-size:14px; color:var(--accent); }} .sec p {{ margin:0; font-size:13.5px; }}
.scroll {{ overflow-x:auto; }}
table.comp {{ border-collapse:collapse; width:100%; font-size:13px; min-width:1100px; }}
table.comp th, table.comp td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; }}
table.comp thead th {{ background:var(--card); border-bottom:2px solid var(--line); }}
table.comp td.num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
ul.excl {{ padding-left:18px; }} ul.excl li {{ margin:6px 0; font-size:13.5px; }}
.ask {{ background:var(--card); border:2px solid var(--accent); border-radius:14px; padding:20px 22px; margin:34px 0 0; }}
.ask h2 {{ border:0; margin:0 0 8px; }}
code {{ background:color-mix(in srgb, var(--ink) 8%, transparent); padding:1px 5px; border-radius:4px; font-size:12px; }}
.foot {{ color:var(--muted); font-size:12px; margin-top:40px; border-top:1px solid var(--line); padding-top:12px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>{title}</h1>
<p class="lede">screening基準日 {asof}／注文想定日 {target} ／ これは<b>最終buy提案ではありません</b>。表示価格はscreeningの評価用参考価格で、発注に使うJPX raw/unadjusted closeは深掘り後のplan-limitで別に取得します。</p>

<div class="brief">
<div><span class="pill">目的</span> 永久的資本毀損リスクを抑えつつ、一時的に大きく割安になっている日本株を見つける。提示順は{order_by}（推奨順位ではありません）。</div>
{intro_block}
</div>

<h2>{n}候補（第1層データ＋選定背景）</h2>
{cards}

<h2>{n}候補 比較表（軸を分離）</h2>
<div class="scroll">
<table class="comp">
<thead><tr>
<th>#</th><th>ticker / 銘柄</th><th>screening参考価格</th><th>valuation</th><th>永久損失(暫定)</th>
<th>自己資本比率 / net cash比</th><th>機械E[r]</th><th>E[r]分解(rev/carry)</th><th>60日変化</th><th>FV乖離</th><th>次回決算</th><th>暫定判断</th>
</tr></thead>
<tbody>
{comp_rows}
</tbody>
</table>
</div>
<p class="lede">注: 機械E[r]・FVアンカーは screening の機械見積り（reversion + carry）であり<b>事実ではありません</b>。FV乖離は「FVアンカー/現値−1」。永久損失は第1段階の暫定読みで、7軸の本評価は第2段階。E[r]分解のreversionは価格の異常乖離の回帰、carryは配当等の継続リターン推定。carry偏重のE[r]は割安の証拠ではない。</p>

<h2>audit pool 上位のうち 非選択の理由</h2>
<ul class="excl">
{excl_rows}
</ul>

<div class="ask">
<h2>ユーザーへの依頼</h2>
<p>この候補から、次に<b>個別リサーチ（深掘り）する銘柄</b>を選んでください。注文承認はまだ求めません。推奨は<b>2〜4銘柄</b>（数はhard ruleではありません）。</p>
<p>選択後、その銘柄について最新決算短信・有報・説明資料・適時開示・資金繰り・希薄化・7軸・3年/5年シナリオ・FV・独立反証・最終比較・指値proposalを行います。</p>
</div>

<div class="foot">
screening candidates sha256: <code>{cand_hash}</code><br>
selection-output sha256: <code>{sel_hash}</code><br>
prepared selection sha256: <code>{prepared_hash}</code><br>
selection: {selection_path}
</div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
