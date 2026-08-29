"""Candidate tag and summary rendering for selection payload output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite

from baibai_engine.foundation.coerce import (
    dedupe_strings,
    mapping_or_empty,
    optional_float,
    string_or_none,
    string_sequence,
)

from ..metrics import PRICE_HISTORY_WINDOW_DAYS
from .candidate_diagnostics import _durability_diagnostic_of

# ranked_set の event_warnings は、価格・EPS・配当の fact を歪め得る
# corporate action / 決算跨ぎ / 開示鮮度の risk tag だけを写す。需給系
# (previous_candidate / benchmark_laggard) は event ではないので除く。
_EVENT_RISK_TAGS = frozenset(
    {
        "split_adjustment_recent",
        "earnings_scheduled",
        "freshness_warning",
        "forecast_special_gain",
        "forecast_full_year_loss",
        "stale_financials",
        # 悪化ゲートを判定材料なしで通ったこと。ranked_set 20 件は点検 view で、
        # `risk_tags` を持つのは推奨行だけなので、ここに入れないと点検面の半分に出ない。
        "deterioration_unmeasurable",
    }
)

# Nikkei に 3pt 以上劣後している候補を事前固定の annotation 閾値で注記する。
# 割安 (相対劣後) を買うのが本流のため ranking / gate には使わず、Research Gate
# の情報系列として参照する。
BENCHMARK_LAGGARD_RELATIVE_20D_MAX = -0.03

# 上場 750 暦日以上なのに直近 750 暦日の bar 本数が population 最大の 80% を
# 下回る銘柄は、自己レンジ / sigma gap が前提にする 3 年履歴に長期ギャップが
# ある(新規上場は short_history_flag 側で扱う)。事前固定の annotation 閾値。
PRICE_HISTORY_GAP_COVERAGE_MIN = 0.8
_PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS = PRICE_HISTORY_WINDOW_DAYS


def _durability_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = string_or_none(_durability_diagnostic_of(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    evidence_pattern = string_or_none(candidate.get("primary_evidence_pattern_id"))
    if evidence_pattern:
        tags.append(evidence_pattern)
    durability = _durability_diagnostic_of(candidate)
    rating = string_or_none(durability.get("rating"))
    if rating == "high":
        tags.append("durability_high")
    elif rating == "medium":
        tags.append("durability_medium")
    return dedupe_strings(tags)


def _candidate_risk_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if candidate.get("previous_candidate") is True:
        tags.append("previous_candidate")
    benchmark_relative_20d = candidate.get("benchmark_relative_20d")
    if (
        isinstance(benchmark_relative_20d, int | float)
        and benchmark_relative_20d <= BENCHMARK_LAGGARD_RELATIVE_20D_MAX
    ):
        tags.append("benchmark_laggard_20d")
    coverage = candidate.get("price_history_coverage_750d")
    listing_span_days = candidate.get("listing_span_days")
    if (
        isinstance(coverage, int | float)
        and coverage < PRICE_HISTORY_GAP_COVERAGE_MIN
        and isinstance(listing_span_days, int | float)
        and listing_span_days >= _PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS
    ):
        tags.append("price_history_gap")
    # 分割・併合の直後は master 株数と価格の基準日ずれで market_cap / net_cash 比率 /
    # 価格変化率の fact が歪み得る (corporate action 未反映)。research 側で AP-03 の
    # corporate action check を必ず通すよう triage 段階で注意を立てる。
    if candidate.get("split_adjustment_flag") is True:
        tags.append("split_adjustment_recent")
    # 悪化ゲートは 3 つの YoY がすべて欠測だと判定材料を持たないまま通す。通過は
    # 「悪化していない」ことの観測ではないので、その区別を判断面へ残す。
    if candidate.get("deterioration_gate_unmeasurable") is True:
        tags.append("deterioration_unmeasurable")
    if string_or_none(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    # 会社予想の純利益>経常は特別益をほぼ確定する。forward PER / 予想配当 / E[r] carry
    # が一時益で嵩上げされた value trap を triage で必ず表面化させ、research 側の
    # 会社予想 normalize (経常ベースへの丸め) へ誘導する。
    if mapping_or_empty(candidate.get("metrics")).get("forecast_special_gain_flag") is True:
        tags.append("forecast_special_gain")
    # 会社自身が通期赤字を予想している行。赤字予想は forward PER を落として FV アンカーを
    # 自己履歴 PBR だけにするので、収益基盤が縮んでも帳簿由来の implied upside が残る。
    # 一過性の赤字 (引当・減損) と構造的な縮小をここでは区別できないため、除外でなく
    # 注記にして research の一次資料読みへ渡す。
    if mapping_or_empty(candidate.get("metrics")).get("forecast_full_year_loss_flag") is True:
        tags.append("forecast_full_year_loss")
    # 発表は済んだのに store の開示がそこまで届いていない窓。この行の財務・FV アンカー・
    # E[r] は旧四半期のままなので、一次開示を先に読ませる。
    if mapping_or_empty(candidate.get("metrics")).get("stale_fin_flag") is True:
        tags.append("stale_financials")
    return dedupe_strings(tags)


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    durability_diagnostic = _durability_diagnostic_of(candidate)
    metrics = mapping_or_empty(candidate.get("metrics"))
    summary = {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "sector_33": string_or_none(candidate.get("sector_33")),
        "primary_evidence_pattern_id": string_or_none(candidate.get("primary_evidence_pattern_id")),
        "market_cap_oku": candidate.get("market_cap_oku"),
        # liquidity: 5% ADV 参加上限で発注可能サイズを判断し、約定できない薄商いを弾く
        "avg_turnover_oku": candidate.get("avg_turnover_oku"),
        # valuation: triage 時に割安度を即判断できるよう転記する。ticker-profile を別途引かずに済む
        "per_trailing": candidate.get("per_trailing"),
        "per_forward": candidate.get("per_forward"),
        "pbr": candidate.get("pbr"),
        "ev_ebitda": candidate.get("ev_ebitda"),
        "p_s": candidate.get("p_s"),
        "pcfr": candidate.get("pcfr"),
        # downside protection: net-net / cash-rich の下値判断に使う財務指標
        "cash_to_market_cap": metrics.get("cash_to_market_cap"),
        "net_cash_to_market_cap": metrics.get("net_cash_to_market_cap"),
        "investment_securities": metrics.get("investment_securities"),
        "asset_backed_ratio": metrics.get("asset_backed_ratio"),
        "equity_ratio": metrics.get("equity_ratio"),
        "ocf_yield": metrics.get("ocf_yield"),
        # earnings momentum + cash conversion: 割安な trailing PER が減益・低 cash 変換を
        # 隠す value trap を triage で弾くための質シグナル
        "operating_profit_yoy": metrics.get("operating_profit_yoy"),
        "sales_yoy": metrics.get("sales_yoy"),
        "fcf_yield": metrics.get("fcf_yield"),
        # 機械 E[r] (成分分解付き見積り・%/年の比率) と FV アンカー。thesis の
        # FV 見積りの機械的出発点で、単一の合成スコアではない (estimates.py)。
        "er_annual": metrics.get("er_annual"),
        "er_reversion_annual": metrics.get("er_reversion_annual"),
        "er_carry_annual": metrics.get("er_carry_annual"),
        "er_dividend_yield": metrics.get("er_dividend_yield"),
        "er_anchor_metrics": metrics.get("er_anchor_metrics"),
        "er_origin": metrics.get("er_origin"),
        "er_model_version": metrics.get("er_model_version"),
        "er_unit": metrics.get("er_unit"),
        "er_assumptions": metrics.get("er_assumptions"),
        "fv_sector_median_yen": metrics.get("fv_sector_median_yen"),
        "fv_self_range_yen": metrics.get("fv_self_range_yen"),
        "dps_actual_annual": metrics.get("dps_actual_annual"),
        "dps_forecast_annual": metrics.get("dps_forecast_annual"),
        "dividend_yield": metrics.get("dividend_yield"),
        # 配当利回りをどの経路で作ったか。forecast_annual は予想 DPS、actual_reported は
        # 会計期間に分割・併合が無く短信の年間値をそのまま使った行、
        # actual_record_date_resolved は期間内に分割があり支払ごとに基準日より後の調整を
        # 掛け直した行。unresolved_split_basis は掛け直せず利回りを出していない状態で、
        # 無配 (dividend_yield=0) とも観測できない (unavailable) とも別であり、E[r] も
        # 付かない。carry 支配型ならここが unresolved の銘柄は短信の配当表へ戻る。
        # dividend_split_factor は会計期間に起きた累積 factor (期間内に何も無ければ null)。
        "dividend_basis": metrics.get("dividend_basis"),
        "dividend_split_factor": metrics.get("dividend_split_factor"),
        # 資本配分・支配権イベントの typed fact (capital_control.py が読み、ここは転記だけ)。
        # 価値実現の経路がいつ・誰から来るかの文脈であり、単独で採否を決める材料ではない。
        # None は観測できていない状態で、"none" / false (観測して該当なし) と違う。
        "tse_capital_policy_status": metrics.get("tse_capital_policy_status"),
        "tse_capital_policy_updated_on": metrics.get("tse_capital_policy_updated_on"),
        "large_holding_event_recent": metrics.get("large_holding_event_recent"),
        "large_holding_event_latest_on": metrics.get("large_holding_event_latest_on"),
        "tender_offer_event_recent": metrics.get("tender_offer_event_recent"),
        "tender_offer_event_latest_on": metrics.get("tender_offer_event_latest_on"),
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        # dislocation 深度: 売られすぎ度の主要 window。割安ゾーン入りの経緯と RR の前提
        "price_change_60d": candidate.get("price_change_60d"),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "price_history_coverage_750d": candidate.get("price_history_coverage_750d"),
        "split_adjustment_flag": candidate.get("split_adjustment_flag") is True,
        "next_earnings_date": candidate.get("next_earnings_date"),
        # 決算ラグの annotation (earnings_lag.py が判定し、ここは転記だけ)。
        # next_earnings_date は予定表の日付なので、前倒し開示や当日発表を日付だけでは
        # 読めない。状態・行が含む最後の開示日・カレンダー欠落時の推定日を併記する。
        # stale_fin_flag の None は「判定材料が無い」で、False (照合して一致) と違う。
        "next_earnings_status": metrics.get("next_earnings_status"),
        "next_earnings_estimated_date": metrics.get("next_earnings_estimated_date"),
        "fin_latest_disclosed_date": metrics.get("fin_latest_disclosed_date"),
        "stale_fin_flag": metrics.get("stale_fin_flag"),
        "position_tier": candidate.get("position_tier"),
        "durability_rating": string_or_none(durability_diagnostic.get("rating")),
        "durability_caution_reasons": list(
            string_sequence(durability_diagnostic.get("caution_reasons"))
        ),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }
    summary["decision_input_seed"] = candidate["decision_input_seed"]
    return summary


def _ranked_set_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    """Render one ranked_set row: the pre-shortlist view of a ranked candidate.

    ranked_set は E[r] 順の候補を review cap までそのまま渡す view。
    ranking も candidate の値も変えず、rank と主要な
    見積り・warning だけを平らに写す。約定用の price basis はここでは決めない
    (plan-limit が SQLite の raw close を正本にする)ため、market_price は screening
    の参考値であることを field で明示する。
    """
    metrics = mapping_or_empty(candidate.get("metrics"))
    return {
        **_selection_candidate_summary(candidate, rank=rank),
        **_ranked_set_machine_projection(candidate),
        # carry のもう半分。`dividend_yield` は予想 DPS を現値で割った 1 つの数で、その額が
        # 反復する普通配当なのか一回性の特別配当なのかを区別しない。特別配当は予想年間 DPS へ
        # そのまま入るので、carry が E[r] の主キーである以上、一回性の分配は上位へ集中して
        # 現れる。予想と直近実績を並べて出すのは、その桁の跳ねを判断面で見えるようにするため
        # である。両者は同じ株式基準へ揃えた後の値で、揃えられなかった年度は実績側が null に
        # なる (`basis` がどちらを使ったかを言う)。比率にはしない — 実績側は前年度の値なので、
        # 1 つの数へ畳むと「どの期と比べているか」が消える。
        "dividend_basis": {
            "annual_yield": metrics.get("dividend_yield"),
            "dps_forecast_annual": metrics.get("dps_forecast_annual"),
            "dps_actual_annual": metrics.get("dps_actual_annual"),
            "basis": metrics.get("dividend_basis"),
            "split_factor": metrics.get("dividend_split_factor"),
        },
        # ranked_setはResearch Gateが20件を点検するviewなので、価値実現の経路を示すdated factも
        # ここに置く。rank へは接続しない。
        "capital_control": {
            "tse_capital_policy_status": metrics.get("tse_capital_policy_status"),
            "tse_capital_policy_updated_on": metrics.get("tse_capital_policy_updated_on"),
            "large_holding_event_recent": metrics.get("large_holding_event_recent"),
            "large_holding_event_latest_on": metrics.get("large_holding_event_latest_on"),
            "tender_offer_event_recent": metrics.get("tender_offer_event_recent"),
            "tender_offer_event_latest_on": metrics.get("tender_offer_event_latest_on"),
        },
    }


def _ranked_set_machine_projection(candidate: Mapping[str, object]) -> dict[str, object]:
    """Project every source-derived coordinate burned into a shortlist snapshot."""

    metrics = mapping_or_empty(candidate.get("metrics"))
    durability_diagnostic = _durability_diagnostic_of(candidate)
    risk_tags = list(string_sequence(candidate.get("risk_tags")))
    decision_input_seed = mapping_or_empty(candidate.get("decision_input_seed"))
    seed_estimates = mapping_or_empty(decision_input_seed.get("estimates"))
    return {
        "name": string_or_none(candidate.get("name")),
        # er_annual は annual_ratio (0.1 = 10%/年)。ranked_set view は pct で読むので x100。
        "expected_return_pct": _ratio_to_pct(optional_float(metrics.get("er_annual"))),
        "fair_value_anchor_yen": _conservative_fair_value_yen(metrics),
        # 最後の raw close を as-of の株式基準へ換算した screening 参考値。約定 limit の
        # price basis ではなく、plan-limit は SQLite の raw/unadjusted close を再取得する。
        "market_price_yen": _screening_reference_close_yen(metrics),
        # opportunity thesis-scaffold は ranked_set から選ばれた銘柄も扱うため、
        # raw estimate + provenance contract を渡す。flat fields
        # は人間向け表示であり、転記時の正本にはしない。
        "estimate_snapshot": {
            "as_of": decision_input_seed.get("as_of"),
            **seed_estimates,
        },
        "liquidity_status": "pass",
        "durability_warnings": list(string_sequence(durability_diagnostic.get("caution_reasons"))),
        "event_warnings": [tag for tag in risk_tags if tag in _EVENT_RISK_TAGS],
        "selection_reasons": list(string_sequence(candidate.get("reason_tags"))),
        "fv_convergence": _fv_convergence_annotation(candidate, metrics),
    }


def _ratio_to_pct(value: float | None) -> float | None:
    return round(value * 100, 4) if value is not None else None


def _conservative_fair_value_yen(metrics: Mapping[str, object]) -> float | None:
    anchors = list(_fair_value_anchors(metrics).values())
    return round(min(anchors), 4) if anchors else None


def _screening_reference_close_yen(metrics: Mapping[str, object]) -> float | None:
    price = _positive_finite(metrics.get("market_price_yen"))
    return round(price, 4) if price is not None else None


def _fair_value_anchors(metrics: Mapping[str, object]) -> dict[str, float]:
    # FV の per-share basis は同じ artifact の as-of-basis 参考価格で証明する。
    # 価格 basis を証明できない anchor は、自己株控除後 market cap と gross shares の混在を
    # 防ぐため判断面へ出さない。
    if _screening_reference_close_yen(metrics) is None:
        return {}
    return {
        key: anchor
        for key in ("fv_sector_median_yen", "fv_self_range_yen")
        if (anchor := _positive_finite(metrics.get(key))) is not None
    }


def _fv_convergence_annotation(
    candidate: Mapping[str, object], metrics: Mapping[str, object]
) -> dict[str, object]:
    """Describe FV convergence without changing selection authority.

    Every usable anchor must be exhausted. This avoids calling a candidate converged
    when the two machine anchors disagree and one still offers upside. The independent
    reversion sign is a consistency guard against turning the displayed FV comparison
    into a new estimate policy.
    """
    price = _screening_reference_close_yen(metrics)
    anchors = _fair_value_anchors(metrics)
    reversion = _finite_number(metrics.get("er_reversion_annual"))
    if price is None or not anchors or reversion is None:
        status = "not_evaluable"
        warning_code = None
    elif all(price >= anchor for anchor in anchors.values()) and reversion <= 0:
        status = "warning"
        warning_code = "price_at_or_above_all_fv_anchors"
    else:
        status = "clear"
        warning_code = None
    return {
        "status": status,
        "warning_code": warning_code,
        "market_price_yen": price,
        "anchors_yen": anchors,
        "er_reversion_annual": reversion,
    }


def _positive_finite(value: object) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _finite_number(value: object) -> float | None:
    number = optional_float(value)
    return number if number is not None and isfinite(number) else None


def _decision_input_seed(candidate: Mapping[str, object], *, asof_date: date) -> dict[str, object]:
    metrics = mapping_or_empty(candidate.get("metrics"))
    valuation = {
        key: candidate[key]
        for key in ("per_trailing", "per_forward", "pbr", "ev_ebitda", "p_s", "pcfr")
        if candidate.get(key) is not None
    }
    return {
        "snapshot_version": 1,
        "producer_model_version": "screening-selection-v1",
        "ticker": string_or_none(candidate.get("ticker")),
        "company_name": string_or_none(candidate.get("name")),
        "as_of": asof_date.isoformat(),
        "local_data_provenance": {
            "provider": "baibai-loop",
            "dataset": "screening-selection",
        },
        "completeness": "ready_for_enrichment" if valuation else "missing_valuation",
        "required_enrichment": ["market_price", "primary_financials"],
        "valuation": valuation,
        "derived": {
            key: candidate.get(key)
            for key in (
                "price_change_5d",
                "price_change_20d",
                "price_change_60d",
                "benchmark_relative_20d",
                "gap_from_52w_low",
            )
            if candidate.get(key) is not None
        },
        "estimates": {
            "expected_return": {
                key.removeprefix("er_"): metrics.get(key)
                for key in (
                    "er_annual",
                    "er_reversion_annual",
                    "er_carry_annual",
                    "er_dividend_yield",
                    "er_anchor_metrics",
                    "er_origin",
                    "er_model_version",
                    "er_unit",
                    "er_assumptions",
                )
                if metrics.get(key) is not None
            },
            "fair_value": {
                "anchors": _fair_value_anchors(metrics),
                "origin": metrics.get("er_origin"),
                "model_version": metrics.get("er_model_version"),
                "unit": "JPY_per_share",
                "assumptions": metrics.get("er_assumptions"),
            },
        },
    }
