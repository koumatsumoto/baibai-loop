"""当時の選定で固定した銘柄情報と、報告用の限定的な見立て。採否/点数は作らない。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from baibai_engine.investment_judgment.cash_flow_use import CashFlowLimit, cash_flow_limit
from baibai_engine.machine_analysis.models.company_financials import REPORTED_PROFIT_BASIS
from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment
from baibai_engine.primitives.time import JST

Row = dict[str, Any]
Direction = Literal["supportive", "mixed", "adverse", "neutral", "context_only", "insufficient"]
MODELS = (
    "company_financials",
    "company_growth",
    "market_context",
    "external_assessment",
    "technical_analysis",
    "industry_trends",
    "social_trends",
)
# L2観測の欠測と、必要時だけ調べるL3観点の未記録を区別する。
_OBSERVATION_GAPS = {
    "company_financials": "同じ企業の実績利益・営業キャッシュフローを期間付きで確認できません。",
    "company_growth": "当該企業の直近の前年同期比較を同一基準で確認できません。",
    "market_context": "市場代理の騰落や時点の合う経済観測を確認できません。",
    "external_assessment": "取得時刻と銘柄を照合できる外部の目標株価・算定値がありません。",
    "technical_analysis": "対象銘柄または同業の価格観測を確認できません。",
}


def _obj(x: object) -> Row:
    return x if isinstance(x, dict) else {}


def _rows(x: object) -> list[Row]:
    return [v for v in x if isinstance(v, dict)] if isinstance(x, list) else []


def _at(x: object, *keys: str) -> Any:
    for key in keys:
        x = _obj(x).get(key)
    return x


def _num(x: object) -> float | None:
    if type(x) not in (float, int) or not isinstance(x, (float, int)):
        return None
    try:
        return float(x) if math.isfinite(x) else None
    except OverflowError:
        return None


def _stamp(x: object, *, native_jst: bool = False) -> datetime | None:
    if not isinstance(x, str):
        return None
    try:
        dt = datetime.fromisoformat(x)
    except ValueError:
        return None
    if dt.utcoffset() is not None:
        return dt
    # EDINETの提出日時は現地の時分。日付だけをその日0時の開示とは扱わない。
    return dt.replace(tzinfo=JST) if native_jst and len(x) >= 16 and x[10] in (" ", "T") else None


def _day(x: object, limit: date) -> date | None:
    if not isinstance(x, str):
        return None
    try:
        d = date.fromisoformat(x)
    except ValueError:
        return None
    return d if d <= limit else None


@dataclass(frozen=True)
class Reading:
    assessment: Direction
    summary: str
    evidence: str
    observed_on: date | None
    source_url: str | None = None


def _with_context(primary: Reading | None, context: Reading | None) -> Reading | None:
    """付記の観測期間を本文に残し、主観測の方向・日付を変更しない。"""
    if primary is None:
        return context
    if context is None:
        return primary
    return replace(
        primary,
        summary=f"{primary.summary} {context.summary}",
        evidence=f"{primary.evidence}／{context.evidence}",
        source_url=primary.source_url or context.source_url,
    )


def _link(raw: object, *trusted_hosts: str) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        url = urlsplit(raw)
        if (
            url.scheme == "https"
            and url.hostname in trusted_hosts
            and url.netloc == url.hostname
            and all(ord(c) > 32 for c in raw)
        ):
            return raw
    except ValueError:
        pass
    return None


def _own(raw: object, ticker: str) -> Row:
    v = _obj(raw)
    return v if v.get("ticker", ticker) == ticker else {}


def _signals(pairs: list[tuple[float, str]], note: str, source: str, day: date) -> Reading | None:
    if not pairs:
        return None
    high, low = any(x > 0 for x, _ in pairs), any(x < 0 for x, _ in pairs)
    judgment: Direction = (
        "mixed" if high and low else "supportive" if high else "adverse" if low else "neutral"
    )
    return Reading(judgment, "、".join(text for _, text in pairs) + "。" + note, source, day)


def _components_known(components: object, limit: date, cutoff: datetime) -> bool:
    if components is None:
        return True  # Unrecorded vintage in an older exact snapshot; do not invent a clock.
    if not isinstance(components, list):
        return False
    for component in components:
        if not isinstance(component, dict):
            return False
        if component.get("period_end") is not None and not _day(component["period_end"], limit):
            return False
        disclosed, clock = component.get("disclosed_at"), component.get("disclosed_time")
        if disclosed is None:
            if clock is not None:
                return False
            continue
        day = _day(disclosed, limit)
        if not day:
            return False
        if clock is not None:
            if not isinstance(clock, str):
                return False
            try:
                at = time.fromisoformat(clock)
            except ValueError:
                return False
            if at.tzinfo is not None or datetime.combine(day, at, JST) > cutoff:
                return False
    return True


def _edinet_cash_known(fin: Row, limit: date, received_by: datetime) -> bool:
    snapshot_day = fin.get("edinet_snapshot_asof")
    if snapshot_day is not None and not _day(snapshot_day, limit):
        return False
    submitted = fin.get("edinet_source_submit_datetime")
    received = fin.get("edinet_snapshot_recorded_at_utc")
    filed_at, received_at = _stamp(submitted, native_jst=True), _stamp(received)
    if submitted is not None and (filed_at is None or filed_at > received_by):
        return False
    if received is not None and (received_at is None or received_at > received_by):
        return False
    return filed_at is None or received_at is None or filed_at <= received_at


def _finance(
    f: Row,
    legacy: Row,
    limit: date,
    cutoff: datetime,
    cash_limit: CashFlowLimit | None,
) -> Reading | None:
    ep, fin = _obj(f.get("earnings_price")), _obj(f.get("financial")) or legacy
    when = _day(ep.get("price_date"), limit)
    profit, yld = _num(ep.get("profit_ttm_yen")), _num(ep.get("earnings_yield"))
    if (
        when
        and ep.get("unavailable_reason") is None
        and profit is not None
        and yld is not None
        and _components_known(ep.get("profit_components"), limit, cutoff)
    ):
        components = _rows(ep.get("profit_components"))
        ends = [_day(v.get("period_end"), limit) for v in components]
        last = max((v for v in ends if v), default=None) if all(ends) else None
        term = f"{last}までの12か月" if last else "直近12か月・実績期末未記録"
        statement = ep.get("statement_basis")
        profit_name = (
            "親会社帰属利益"
            if statement == "consolidated"
            else "単体純利益"
            if statement == "nonconsolidated"
            else "報告純利益"
        )
        allocation = (
            "、普通株への配分前" if f.get("earnings_basis") == REPORTED_PROFIT_BASIS else ""
        )
        values = [
            (
                profit,
                (
                    f"{profit_name}（{term}{allocation}）{profit / 1e8:+,.2f}億円、"
                    f"{when}の時価総額比{yld:.1%}"
                ),
            )
        ]
        source = "企業財務：earnings_price.profit_ttm_yen / earnings_yield"
        # Only pair cash and reported profit when the retained accounts and periods agree.
        cf, basis = _num(fin.get("ocf_ttm")), _obj(fin.get("ocf_ttm_period"))
        end = _day(basis.get("end"), limit)
        if (
            cf is not None
            and end
            and end == last
            and _components_known(basis.get("components"), limit, cutoff)
            and ep.get("statement_basis") in ("consolidated", "nonconsolidated")
            and basis.get("statement_basis") == ep.get("statement_basis")
            and ep.get("accounting_standard")
            and basis.get("accounting_standard") == ep.get("accounting_standard")
        ):
            reported = f"同じ期末の12か月営業CF {cf / 1e8:+,.2f}億円（{end}まで）"
            source += " / ocf_ttm"
            if cash_limit is not None:
                # 開示額は残すが、金融業の資金の増減を利益に賛否を示す票にはしない。
                return _signals(
                    values,
                    f"実績利益の符号で、将来の手取りではありません。{reported}は参考。"
                    f"{cash_limit.reading}",
                    source,
                    when,
                )
            values.append((cf, reported))
        return _signals(
            values,
            f"実績利益の符号で、将来の手取りではありません。{cash_limit.reading}"
            if cash_limit is not None and cash_limit.sector is not None
            else "実績の符号であり、将来利益や設備投資・分配後の手残りではありません。",
            source,
            when,
        )
    when = _day(
        fin.get("latest_financial_disclosure_date") or fin.get("fin_latest_disclosed_date"), limit
    )
    op = _num(fin.get("operating_profit_ttm"))
    if when and op is not None:
        return _signals(
            [(op, f"直近12か月の営業利益 {op / 1e8:+,.2f}億円")],
            "営業黒字・赤字を表し、割安さや配当余力は別の判断です。",
            "企業財務：operating_profit_ttm（表示日は直近の財務開示日）",
            when,
        )
    equity = _num(fin.get("equity_ratio"))
    if when and equity is not None:
        return Reading(
            "adverse" if equity < 0 else "context_only",
            f"保存された自己資本比率 {equity:.1%}。"
            "業種や資産の質に依存するため、これだけで資金の余裕や株主価値を決めません。",
            "企業財務：equity_ratio（表示日は直近の財務開示日）",
            when,
        )
    # An old EDINET cash-flow series has its own fiscal end; do not date it as the latest results.
    day = _day(fin.get("edinet_source_period_end"), limit)
    cf = _num(fin.get("edinet_ocf_ttm"))
    if day and cf is not None and _edinet_cash_known(fin, limit, cutoff):
        if cash_limit is not None:
            return Reading(
                "context_only",
                f"報告営業キャッシュフロー {cf / 1e8:+,.2f}億円（元開示の期末{day}）。"
                f"{cash_limit.reading}",
                "企業財務：edinet_ocf_ttm / edinet_source_period_end",
                day,
            )
        return _signals(
            [(cf, f"営業キャッシュフロー（直近12か月）{cf / 1e8:+,.2f}億円")],
            "実績の資金流出入。設備投資や株主分配後の手残りではありません。",
            "企業財務：edinet_ocf_ttm / edinet_source_period_end",
            day,
        )
    return None


def _growth(g: Row, legacy: Row, limit: date) -> Reading | None:
    if not g:
        day = _day(legacy.get("fin_latest_disclosed_date"), limit)
        value = _num(legacy.get("sales_yoy"))
        if day and value is not None:
            return _signals(
                [(value, f"保存された売上高の前年同期比 {value:+.1%}")],
                "売上のみの観測で、利益成長や継続性までは確定しません。",
                "旧企業財務：sales_yoy（表示日は直近の財務開示日）",
                day,
            )
        return None
    changes = [
        v
        for v in _rows(g.get("changes"))
        if v.get("measurement") == "quarter"
        and v.get("comparison") == "year_over_year"
        and v.get("metric") in ("sales", "operating_profit")
    ]
    # Do not fall back to an older favorable quarter when the newest quarter is incomplete.
    ends = [d for v in _rows(g.get("periods")) if (d := _day(v.get("period_end"), limit))]
    ends += [d for v in changes if (d := _day(v.get("to_end"), limit))]
    if not ends:
        return None
    day = max(ends)
    signals = []
    for key, label in (("sales", "売上"), ("operating_profit", "営業利益")):
        vs = [v for v in changes if v.get("metric") == key and v.get("to_end") == day.isoformat()]
        if len(vs) != 1 or _at(g, "metric_units", key) != "JPY":
            continue
        v = vs[0]
        prior = _day(v.get("from_end"), day)
        new, old = _num(v.get("to_value")), _num(v.get("from_value"))
        if (
            v.get("reason") not in (None, "nonpositive_base")
            or not prior
            or prior >= day
            or new is None
            or old is None
            or _num(v.get("difference")) is None
        ):
            continue
        delta = new - old
        if not math.isclose(delta, v["difference"], rel_tol=1e-10, abs_tol=0.01):
            continue
        signals.append((delta, f"{label} {old / 1e8:,.2f}→{new / 1e8:,.2f}億円"))
    if not signals and any(
        v.get("to_end") == day.isoformat() and v.get("reason") == "accounting_standard_incomparable"
        for v in changes
    ):
        return Reading(
            "insufficient",
            "直近四半期は前年同期と会計基準が異なり、実額があっても同一基準の増減を判定できません。",
            "企業成長：quarter / year_over_year（accounting_standard_incomparable）",
            day,
        )
    return _signals(
        signals,
        "直近四半期の前年同期比。事業売却・新作等の反動はこの比較だけでは除けません。",
        "企業成長：quarter / year_over_year（各期の実額）",
        day,
    )


def _market_proxy(m: Row, limit: date) -> Reading | None:
    points = [
        (_day(v.get("observation_date"), limit), v)
        for v in _rows(m.get("points"))
        if v.get("date") is None or _day(v.get("date"), limit)
    ]
    dates = [d for d, _ in points if d]
    if not dates or not m.get("benchmark_ticker"):
        return None
    day = max(dates)
    vs = [v for d, v in points if d == day]
    if len(vs) != 1:
        return None
    values = [
        f"市場代理{m['benchmark_ticker']}の終値・{n}本前比 {v:+.1%}"
        for n in (20, 60)
        if (v := _num(vs[0].get(f"benchmark_return_{n}d"))) is not None
    ]
    if not values:
        return None
    # ETFの価格観測を、個社への合成方向判定にしない。
    return Reading(
        "context_only",
        "、".join(values)
        + "。この代理の価格変化です。他の指数や個社が同じ方向・幅で動くとは限りません。",
        "市場環境：market.points / benchmark_return_20d・60d",
        day,
    )


def _technical(t: Row, sec: Row, limit: date) -> Reading | None:
    ma, mo = _obj(_at(t, "moving_averages", "sma_20bar")), _obj(t.get("price_momentum"))
    gap, mom = _num(ma.get("gap_ratio")), _num(mo.get("value"))
    d, end = _day(ma.get("window_end_date"), limit), _day(mo.get("end_date"), limit)
    pairs = []
    if ma.get("status") == "available" and d and gap is not None:
        pairs.append((gap, f"20立会平均からの乖離 {gap:+.1%}（{d}終点）"))
    if mo.get("status") == "available" and end and mom is not None:
        pairs.append((mom, f"直近1か月を除く12か月モメンタム {mom:+.1%}（{end}終点）"))
    if pairs:
        return _signals(
            pairs,
            "価格の方向で、事業価値や今後の収益を示すものではありません。",
            "テクニカル：sma_20bar.gap_ratio / price_momentum",
            max(
                day
                for day, ok in (
                    (d, ma.get("status") == "available" and gap is not None),
                    (end, mo.get("status") == "available" and mom is not None),
                )
                if day and ok
            ),
        )
    p = _obj(sec.get("price"))
    day = _day(p.get("resolved_date"), limit)
    if not day:
        return None
    pairs = [
        (v, f"自社株・{n}立会 {v:+.1%}")
        for n in (20, 60)
        if (v := _num(p.get(f"return_{n}d"))) is not None
    ]
    return _signals(
        pairs,
        "移動平均の代わりに当時の実現騰落を参照。約定・将来収益の予想ではありません。",
        "個社価格：price.return_20d・60d",
        day,
    )


def _external(e: Row, ticker: str, limit: date, cutoff: datetime) -> Reading | None:
    if e.get("ticker") != ticker:
        return None
    for row in _rows(e.get("model_valuations")):
        obs = _obj(_at(row, "inputs", "latest"))
        stamp, day = _stamp(obs.get("fetched_at_utc")), _day(obs.get("snapshot_date"), limit)
        if (
            not stamp
            or stamp > cutoff
            or not day
            or obs.get("ticker") != ticker
            or obs.get("currency") != "JPY"
            or obs.get("unit") != "JPY/share"
        ):
            continue
        for key, label in (
            ("consensus_target", "コンセンサス目標"),
            ("per_company_forecast", "外部試算（PER方式、利益は発行会社の予想）"),
            ("pbr", "PBR方式の外部試算"),
        ):
            value, origin = _num(obs.get(key)), obs.get(f"{key}_origin")
            if value is None or value <= 0 or not isinstance(origin, str) or not origin:
                continue
            basis = "株数基準未確認。" if not obs.get("share_basis") else ""
            kind = "PBR倍率ではなく外部モデルの一株価格です。" if key == "pbr" else ""
            return Reading(
                "context_only",
                f"{origin}の{label} {value:,.0f}円/株。{kind}{basis}当時の参考値で、"
                "現在の同意見・独立した適正価値・買い推奨とはみなしません。",
                f"外部評価：{origin} / {key}（取得 {stamp.isoformat()}）",
                day,
                _link(obs.get("source_url"), "scouter.monex.co.jp"),
            )
    return None


def _article(
    payload: Row, domain: str, scope_id: str, limit: date, cutoff: datetime
) -> Reading | None:
    valid = []
    for v in _rows(payload.get("observations")):
        c, e, ref = _obj(v.get("claim")), _obj(v.get("evidence")), _obj(v.get("exact_evidence"))
        stamp = _stamp(e.get("received_at"))
        day = _day(c.get("occurred_on") or _at(c, "scope", "period_end"), limit)
        q = _obj(c.get("quantity"))
        if (
            not stamp
            or stamp > cutoff
            or not day
            or not isinstance(ref.get("observation_sha256"), str)
            or len(ref["observation_sha256"]) != 64
            or ref["observation_sha256"] != e.get("observation_sha256")
            or c.get("domain") != domain
            or _at(c, "scope", "scope_id") != scope_id
            or c.get("kind") != "reported_fact"
            or c.get("negated") is not False
            or c.get("status") not in ("decided", "reported")
            or not isinstance(c.get("attribution"), str)
            or not isinstance(q.get("value"), str)
            or q.get("unit") != "percent"
        ):
            continue
        try:
            amount = float(q["value"])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(amount):
            continue
        if (
            domain == "social"
            and c.get("predicate") == "overnight_call_rate_target"
            and _at(c, "scope", "target") == "uncollateralized_overnight_call_rate"
        ):
            phrase = "無担保コール翌日物の誘導目標"
        elif domain == "industry" and c.get("predicate") == "profitable_company_share":
            label = _at(c, "scope", "label")
            if not isinstance(label, str) or not label:
                continue
            size = _at(c, "scope", "population_size")
            sample = f"調査対象{size:,}社" if type(size) is int and size > 0 else "調査対象"
            phrase = f"{label}の{sample}に占める黒字企業"
        else:
            continue
        note = (
            "。金融政策の事実で、預金・借入、需要を通じたこの会社への純影響は未確認。"
            if domain == "social"
            else "。調査対象の集計で、個社の利益変化に直結させません。"
        )
        valid.append(
            (
                day,
                Reading(
                    "context_only",
                    f"{day} {c['attribution']}：{phrase} "
                    f"{'約' if q.get('approximate') else ''}{amount:g}%{note}",
                    f"{c['attribution']}・保存記事 {ref['observation_sha256'][:12]}"
                    f"（受領 {stamp.isoformat()}）",
                    day,
                    _link(ref.get("original_url"), "www.boj.or.jp", "www.tsr-net.co.jp"),
                ),
            )
        )
    return max(valid, key=lambda x: x[0])[1] if valid else None


def _household_economy(market: Row, limit: date, cutoff: datetime) -> Reading | None:
    """A retained national household statistic is a context, not a company-specific verdict."""
    series = _rows(_at(market, "reading", "series"))
    for key, label, unit, display in (
        ("jp.cpi.core_yoy", "日本の生鮮食品を除く消費者物価・前年同月比", "percent", "%"),
        ("jp.consumer_confidence", "日本の消費者態度指数", "pt", "ポイント"),
    ):
        found = [v for v in series if v.get("series_id") == key]
        if len(found) != 1:
            continue
        v = found[0]
        day, value = _day(v.get("observed_at"), limit), _num(v.get("latest_value"))
        vintage = _stamp(v.get("vintage_at"))
        if (
            not day
            or value is None
            or v.get("unit") != unit
            or v.get("frequency") != "monthly"
            or v.get("geography") != "japan"
            or v.get("stale") is not False
            or (v.get("vintage_at") is not None and (not vintage or vintage > cutoff))
        ):
            continue
        receipt = f"記録版 {vintage.isoformat()}" if vintage else "統計版の取得時刻は未記録"
        return Reading(
            "context_only",
            f"{day:%Y年%m月}の{label}は{value:g}{display}。家計環境の補助観測。"
            "この会社の値上げ・販売量・利益への影響は未確認です。",
            f"市場環境：reading.series / {key}（{receipt}、対象月の月初表記）",
            day,
        )
    return None


def _sector_price(sec: Row, limit: date) -> Reading | None:
    peer = _obj(_at(sec, "relative", "sector"))
    sector = _at(sec, "master", "sector_33")
    start, end = (
        _day(peer.get("comparison_start_date"), limit),
        _day(peer.get("comparison_end_date"), limit),
    )
    val, n = _num(peer.get("peer_median_return_20d")), peer.get("peer_count")
    if (
        not isinstance(sector, str)
        or peer.get("sector_33") != sector
        or val is None
        or type(n) is not int
        or n <= 0
        or not start
        or not end
        or start >= end
    ):
        return None
    gap = _num(peer.get("relative_return_20d"))
    comparison = (
        f"同期間の差（自社の騰落−同業中央値）は{gap * 100:+.1f}ポイント。"
        if gap is not None
        else ""
    )
    return Reading(
        "context_only",
        f"市場で見る{sector}（{start}〜{end}）：自社を除く{n}社の価格騰落の中央値 {val:+.1%}。"
        f"{comparison}業種の価格動向で、事業の需要・採算を実測したものではありません。",
        "テクニカルの補助観測：個社market_context.relative.sector（業界実体調査とは別）",
        end,
    )


@dataclass(frozen=True)
class _FixedTarget:
    inputs: Row
    version: int
    cutoff: datetime
    research_as_of: date
    model_as_of: date
    entries: list[Row]

    def model(self, key: str, subject: str) -> Row:
        rows = [r for r in self.entries if r.get("model_id") == key and r.get("subject") == subject]
        return _own(rows[0].get("payload"), subject) if len(rows) == 1 else {}

    def market(self, ticker: str) -> Row:
        if self.version == 2:
            stock = _own(_at(self.inputs, "securities_context", ticker), ticker)
            return _own(stock.get("market_context"), ticker)
        return self.model("market_context", ticker)


def _fixed_target(selection: Row, ticker: str) -> _FixedTarget | None:
    # The caller verifies the as-published selection hash. A later name or model is not a fallback.
    version = selection.get("schema_version")
    cutoff = _stamp(selection.get("input_known_at"))
    if (
        version not in (2, 3, 4, 5)
        or not cutoff
        or not any(t.get("ticker") == ticker for t in _rows(selection.get("targets")))
    ):
        return None
    limit = _day(selection.get("research_as_of"), cutoff.astimezone(JST).date())
    if not limit:
        return None
    i = _obj(selection.get("inputs"))
    g = _obj(i.get("generation"))
    model_as_of = limit
    if version != 2:
        issued = _stamp(i.get("screening_published_at"))
        if i.get("screening_published_at") is not None and (not issued or issued > cutoff):
            return None
        clock = _stamp(g.get("evaluation_time"))
        day = _day(g.get("as_of"), limit)
        if not day or not clock or clock > cutoff:
            return None
        model_as_of = min(day, clock.astimezone(JST).date())
    return _FixedTarget(i, version, cutoff, limit, model_as_of, _rows(g.get("models")))


def saved_master_identity(
    selection: Row, *, ticker: str, proposal_as_of: date, prepared_at: datetime
) -> tuple[str, str | None] | None:
    """The issuer label for an unfinished target comes from its own dated master row only."""
    target = _fixed_target(selection, ticker)
    if target is None or target.cutoff > prepared_at or target.research_as_of > proposal_as_of:
        return None
    master = _own(target.market(ticker).get("master"), ticker)
    name, sector = master.get("name"), master.get("sector_33")
    if not _day(master.get("snapshot_date"), target.model_as_of) or not isinstance(name, str):
        return None
    if not name.strip():
        return None
    return name, sector if isinstance(sector, str) and sector.strip() else None


def estimate_missing(
    selection: Row, *, ticker: str, recorded: set[str], selection_sha256: str
) -> Row | None:
    """Caller verifies the exact as-published selection; no current DB, ledger or price access."""
    if (target := _fixed_target(selection, ticker)) is None:
        return None
    version, cutoff, limit = target.version, target.cutoff, target.research_as_of
    i, model = target.inputs, target.model
    g = _obj(i.get("generation"))
    generation_id = g.get("generation_id")
    chart: Row = {
        "selection_sha256": selection_sha256,
        "input_known_at": cutoff,
        "research_as_of": limit,
        "generation_id": generation_id,
        "rows": [],
    }
    external = model("external_assessment", ticker)
    if version == 5 and external.get("monex_fiscal_eps") is not None:
        try:
            fixed = ExternalAssessment.model_validate(external)
        except ValidationError:
            pass
        else:
            eps = fixed.monex_fiscal_eps
            clock = _stamp(g.get("evaluation_time"))
            if (
                eps is not None
                and eps.reference_eps is not None
                and clock is not None
                and fixed.evaluated_at <= clock
                and eps.received_at_utc.astimezone(JST).date() <= target.model_as_of
            ):
                chart["monex_fiscal_eps"] = {
                    "source_url": eps.source_url,
                    "received_at_utc": eps.received_at_utc,
                    **eps.reference_eps.model_dump(mode="json", exclude={"rolling_one_year"}),
                }
    if set(MODELS) <= recorded:
        return chart if "monex_fiscal_eps" in chart else None
    legacy = {}
    sec = target.market(ticker)
    financial_clock = _stamp(g.get("evaluation_time"))
    financial_day = target.model_as_of
    if version == 2:
        analysis = _obj(i.get("analysis"))
        financial_clock = _stamp(analysis.get("run_at"))
        analysis_day = _day(analysis.get("analysis_as_of"), limit)
        if analysis_day and financial_clock and financial_clock <= cutoff:
            financial_day = min(analysis_day, financial_clock.astimezone(JST).date())
            analysis_row = _own(_at(analysis, "analyses", ticker), ticker)
            legacy = _own(analysis_row.get("metrics"), ticker)
        own = _own(_at(i, "securities_context", ticker), ticker)
        global_market = _obj(i.get("market_context"))
        f, growth, ext, technical = (
            {},
            *(
                _own(own.get(k), ticker)
                for k in ("company_growth", "external_assessment", "technical_analysis")
            ),
        )
    else:
        global_market = model("market_context", "market")
        f, growth, ext, technical = (
            model(k, ticker)
            for k in (
                "company_financials",
                "company_growth",
                "external_assessment",
                "technical_analysis",
            )
        )
        # This was the original identifier in the v3/v4 receipts before the identity migration.
        # Only original accounting fields, never ranks, modeled returns, or migrated bytes.
        old_financial = model("company_valuation", ticker) if version in (3, 4) else {}
        if old_financial:
            f = {} if f else old_financial
    peers = _at(g, "subject_plan", ticker, "related_subjects")
    industry_facts = [
        r
        for s in (peers if isinstance(peers, list) else [])
        if isinstance(s, str)
        and s.startswith("industry:")
        and (
            r := _article(
                model("industry_trends", s), "industry", s.removeprefix("industry:"), limit, cutoff
            )
        )
        is not None
    ]
    readings = (
        _finance(
            f,
            legacy,
            financial_day,
            min(financial_clock, cutoff) if financial_clock else cutoff,
            cash_flow_limit(sec, ticker=ticker, as_of=target.model_as_of),
        ),
        _growth(growth, legacy, limit),
        _with_context(
            _market_proxy(_obj(global_market.get("market")), limit),
            _article(
                model("social_trends", "market:global"),
                "social",
                "jp-monetary-policy",
                limit,
                cutoff,
            )
            or _household_economy(global_market, limit, cutoff),
        ),
        _external(ext, ticker, limit, cutoff),
        _with_context(_technical(technical, sec, limit), _sector_price(sec, limit)),
        max(industry_facts, key=lambda r: r.observed_on or date.min) if industry_facts else None,
        None,  # 社会動向の作者判断に、市場統計や政策決定を自動で代入しない。
    )
    rows = []
    for m, r in zip(MODELS, readings, strict=True):
        if m in recorded:
            continue
        if r is None:
            if (gap := _OBSERVATION_GAPS.get(m)) is None:
                continue
            r = Reading(
                "insufficient",
                gap,
                "当該提案の固定入力内にある、時点・対象を照合できた根拠に限定",
                None,
            )
        rows.append(
            {
                "model": m,
                "assessment": r.assessment,
                "summary": r.summary,
                "evidence": r.evidence,
                "observed_on": r.observed_on,
                "source_url": r.source_url,
            }
        )
    return {**chart, "rows": rows} if rows or "monex_fiscal_eps" in chart else None
