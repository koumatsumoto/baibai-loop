from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .schema import ScreenedRunDocument, ScreenedTicker, TTMQuality

JST = timezone(timedelta(hours=9))
_DECIMAL_PLACES = {
    "per_forward": 2,
    "per_trailing": 2,
    "pbr": 2,
    "ev_ebitda": 1,
    "p_s": 2,
    "pcfr": 1,
}


class RenderError(ValueError):
    """Raised when screened markdown cannot be rendered safely."""


class QuotedString(str):
    """A YAML string that should always be quoted."""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _quoted_scalar_representer(dumper: yaml.SafeDumper, data: QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_QuotedDumper.add_representer(QuotedString, _quoted_scalar_representer)


def build_output_path(asof_date: date) -> Path:
    return Path("screened") / f"{asof_date:%Y}" / f"{asof_date:%m}" / f"{asof_date:%Y-%m-%d}.md"


def render_screened_markdown(document: ScreenedRunDocument) -> str:
    if document.run_date != document.asof_date:
        raise RenderError("run_date must equal asof_date")
    if document.run_at.tzinfo is None:
        raise RenderError("run_at must be timezone-aware")
    if document.run_at.utcoffset() != JST.utcoffset(None):
        raise RenderError("run_at must use JST (+09:00)")

    front_matter = _build_front_matter(document)
    yaml_text = yaml.dump(
        front_matter,
        Dumper=_QuotedDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    body = _build_body(document)
    return f"---\n{yaml_text}\n---\n\n{body}"


def _build_front_matter(document: ScreenedRunDocument) -> dict[str, Any]:
    front_matter: dict[str, Any] = {}
    front_matter["run_date"] = QuotedString(document.run_date.isoformat())
    front_matter["asof_date"] = QuotedString(document.asof_date.isoformat())
    front_matter["universe_size"] = document.universe_size
    front_matter["filters"] = dict(document.filters.items())
    front_matter["generated_by"] = QuotedString(document.generated_by)
    front_matter["data_sources"] = [QuotedString(source) for source in document.data_sources]
    front_matter["run_at"] = QuotedString(document.run_at.isoformat())
    front_matter["tickers"] = [_build_ticker_entry(ticker) for ticker in document.tickers]
    return front_matter


def _build_ticker_entry(ticker: ScreenedTicker) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    entry["ticker"] = QuotedString(ticker.ticker)
    entry["name"] = QuotedString(ticker.name)
    entry["per_forward"] = _round_value("per_forward", ticker.per_forward)
    entry["per_trailing"] = _round_value("per_trailing", ticker.per_trailing)
    entry["pbr"] = _round_value("pbr", ticker.pbr)
    entry["ev_ebitda"] = _round_value("ev_ebitda", ticker.ev_ebitda)
    entry["p_s"] = _round_value("p_s", ticker.p_s)
    entry["pcfr"] = _round_value("pcfr", ticker.pcfr)
    entry["sector_33"] = QuotedString(ticker.sector_33)
    entry["market_cap_oku"] = ticker.market_cap_oku
    entry["avg_turnover_oku"] = (
        round(ticker.avg_turnover_oku, 1) if ticker.avg_turnover_oku is not None else None
    )
    entry["price_change_60d"] = _round_ratio(ticker.price_change_60d)
    entry["price_change_4w"] = _round_ratio(ticker.price_change_4w)
    entry["sector_relative_strength_percentile"] = _round_ratio(
        ticker.sector_relative_strength_percentile
    )
    entry["metrics_breakdown"] = {
        metric: {
            "sector_median_gap": _round_ratio(values.get("sector_median_gap")),
            "self_range_percentile": _round_ratio(values.get("self_range_percentile")),
            "sigma_gap": _round_ratio(values.get("sigma_gap")),
        }
        for metric, values in ticker.metrics_breakdown.items()
    }
    entry["next_earnings_date"] = (
        QuotedString(ticker.next_earnings_date.isoformat())
        if ticker.next_earnings_date is not None
        else None
    )
    entry["ttm_quality"] = {
        "ev_ebitda": ticker.ttm_quality.get("ev_ebitda", TTMQuality.UNAVAILABLE).value,
        "p_s": ticker.ttm_quality.get("p_s", TTMQuality.UNAVAILABLE).value,
        "pcfr": ticker.ttm_quality.get("pcfr", TTMQuality.UNAVAILABLE).value,
    }
    entry["threshold_hit"] = list(ticker.threshold_hit)
    return entry


def _round_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _round_value(metric: str, value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), _DECIMAL_PLACES[metric])


def _build_body(document: ScreenedRunDocument) -> str:
    pass_count = len(document.tickers)
    fact_lines = list(document.fact_memo_lines) or ["該当なし"]
    env_lines = [
        *document.provider_status_lines,
        *document.universe_exclusion_lines,
        _format_ttm_quality_counts(document.ttm_quality_counts),
        *document.fallback_lines,
    ]
    env_lines = [line for line in env_lines if line]
    if not env_lines:
        env_lines = ["データ取得は未実行"]

    lines = [
        f"# Screened: {document.asof_date.isoformat()}",
        "",
        "**成分**: 4 成分アーキテクチャの **(b) スクリーニング通過銘柄**（[`/docs/components/screened.md`](/docs/components/screened.md)）",
        "",
        "**レイヤー**: 事実レイヤー（解釈は入れない）",
        "",
        "**閾値条件**: 以下 3 種の OR 条件、最低 1 つ満たす（[`/docs/screening/mechanical-v1.md`](/docs/screening/mechanical-v1.md)）",
        "",
        "- 条件 A: 業種中央値比 -20% 以上 かつ 過去 3 年自己レンジ下位 20%",
        "- 条件 B: 過去 60 営業日 -15% 以上下落 かつ valuation 1σ 以上下方（業績悪化なし）",
        "- 条件 C: セクター RS 下位 20% + 個別が業種平均下回り（業績悪化なし）",
        "",
        "## 1. 実行概要",
        "",
        f"- 対象営業日: {document.asof_date.isoformat()}",
        f"- Universe サイズ: {document.universe_size} 銘柄",
        f"- 通過銘柄数: {pass_count} 銘柄",
        "",
        "## 2. 通過銘柄の事実メモ",
        "",
    ]
    lines.extend(f"- {line}" for line in fact_lines)
    lines.extend(["", "## 3. 実行環境", ""])
    lines.extend(f"- {line}" for line in env_lines)
    lines.extend(
        [
            "",
            "---",
            "",
            "研究選定は [`/docs/components/research.md`](/docs/components/research.md) の選定プロセスに従う。通過銘柄のうち `view/` で tailwind / neutral の業種/地域のもののみが research 候補となる。",
        ]
    )
    return "\n".join(lines)


def _format_ttm_quality_counts(counts: dict[str, int] | Any) -> str:
    exact = counts.get("exact", 0) if hasattr(counts, "get") else 0
    approximated = counts.get("approximated", 0) if hasattr(counts, "get") else 0
    unavailable = counts.get("unavailable", 0) if hasattr(counts, "get") else 0
    return (
        "ttm_quality 集計: "
        f"exact={exact}, approximated={approximated}, unavailable={unavailable}"
    )
