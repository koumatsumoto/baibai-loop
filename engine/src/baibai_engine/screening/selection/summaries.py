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
from .lenses import _durability_lens_of

# longlist の event_warnings は、価格・EPS・配当の fact を歪め得る
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
    }
)

# Nikkei に 3pt 以上劣後している候補を事前固定の annotation 閾値で注記する。
# 割安 (相対劣後) を買うのが本流のため ranking / gate には使わず、entry
# preflight の情報系列として research 側で参照する。
BENCHMARK_LAGGARD_RELATIVE_20D_MAX = -0.03

# 上場 750 暦日以上なのに直近 750 暦日の bar 本数が population 最大の 80% を
# 下回る銘柄は、自己レンジ / sigma gap が前提にする 3 年履歴に長期ギャップが
# ある(新規上場は short_history_flag 側で扱う)。事前固定の annotation 閾値。
PRICE_HISTORY_GAP_COVERAGE_MIN = 0.8
_PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS = PRICE_HISTORY_WINDOW_DAYS


def _durability_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = string_or_none(_durability_lens_of(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    playbook = string_or_none(candidate.get("selection_playbook"))
    if playbook:
        tags.append(playbook)
    durability = _durability_lens_of(candidate)
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
    durability_lens = _durability_lens_of(candidate)
    metrics = mapping_or_empty(candidate.get("metrics"))
    summary = {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "sector_33": string_or_none(candidate.get("sector_33")),
        "selection_playbook": string_or_none(candidate.get("selection_playbook")),
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
        # 自己株券買付状況報告書の提出観測 (buyback_authorization.py が判定し、ここは転記
        # だけ)。carry の buyback 成分は過去 1 年の株数変化なので、その carry を forward の
        # 現金還元として narrative に書くなら取得期間の終了日と残枠を一次開示で確認する。
        # recent_filing は提出の齢が浅いだけで枠が今も在ることではない。unknown は観測窓が
        # 届いていない状態で、no_filing (窓の中に提出が無い) と違う。
        "buyback_authorization_status": metrics.get("buyback_authorization_status"),
        "buyback_status_latest_filing_date": metrics.get("buyback_status_latest_filing_date"),
        "buyback_status_filing_age_days": metrics.get("buyback_status_filing_age_days"),
        "buyback_status_observed_from": metrics.get("buyback_status_observed_from"),
        # 枠の中身。提出の齢だけでは carry が forward の還元かを答えられないので、決議株式数
        # のうち未取得の割合・直近 3 報告月の取得割合・取得期間の終了日を同じ面へ出す。
        # 終了日が as-of より前なら、提出が新しくても forward の還元は無い。
        "buyback_remaining_share_ratio": metrics.get("buyback_remaining_share_ratio"),
        "buyback_trailing_3m_acquired_ratio": metrics.get("buyback_trailing_3m_acquired_ratio"),
        "buyback_authorization_window_end": metrics.get("buyback_authorization_window_end"),
        "buyback_report_month_end": metrics.get("buyback_report_month_end"),
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
        "durability_rating": string_or_none(durability_lens.get("rating")),
        "durability_caution_reasons": list(string_sequence(durability_lens.get("caution_reasons"))),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }
    summary["decision_input_seed"] = candidate["decision_input_seed"]
    return summary


def _longlist_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    """Render one longlist row: the pre-shortlist view of a ranked candidate.

    longlist は diversity/cap による recommendation 切断 *前* の rank 済み集合を
    そのまま監査するための view。ranking も candidate の値も変えず、rank と主要な
    見積り・warning だけを平らに写す。約定用の price basis はここでは決めない
    (plan-limit が SQLite の raw close を正本にする)ため、market_price は screening
    の参考値であることを field で明示する。
    """
    metrics = mapping_or_empty(candidate.get("metrics"))
    durability_lens = _durability_lens_of(candidate)
    risk_tags = list(string_sequence(candidate.get("risk_tags")))
    decision_input_seed = mapping_or_empty(candidate.get("decision_input_seed"))
    seed_estimates = mapping_or_empty(decision_input_seed.get("estimates"))
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "screening_playbook": string_or_none(candidate.get("selection_playbook")),
        # er_annual は annual_ratio (0.1 = 10%/年)。longlist view は pct で読むので x100。
        "expected_return_pct": _ratio_to_pct(optional_float(metrics.get("er_annual"))),
        "fair_value_anchor_yen": _conservative_fair_value_yen(metrics),
        # run が as-of の raw close として保存した screening 参考値。約定 limit の price
        # basis ではなく、plan-limit は SQLite から同じ raw/unadjusted close を再取得する。
        "market_price_yen": _screening_reference_close_yen(metrics),
        "fv_convergence": _fv_convergence_annotation(candidate, metrics),
        # 自己株券買付状況報告書の提出観測。longlist は OP3 が 20 件を点検する view なので、
        # carry を forward の現金還元として narrative に書けるかの判断材料をここに置く。
        # 提出の齢だけでは答えられない — 枠が満了していれば新しい提出でも forward の還元は
        # 無いので、決議株式数のうち未取得の割合・直近 3 報告月の取得割合・取得期間の終了日を
        # 同じ面へ出す。`window_end` が as-of より前なら carry は過去の資本配分の記録である。
        "buyback_authorization": {
            "status": metrics.get("buyback_authorization_status"),
            "latest_filing_date": metrics.get("buyback_status_latest_filing_date"),
            "filing_age_days": metrics.get("buyback_status_filing_age_days"),
            "observed_from": metrics.get("buyback_status_observed_from"),
            "remaining_share_ratio": metrics.get("buyback_remaining_share_ratio"),
            "trailing_3m_acquired_ratio": metrics.get("buyback_trailing_3m_acquired_ratio"),
            "authorization_window_end": metrics.get("buyback_authorization_window_end"),
            "report_month_end": metrics.get("buyback_report_month_end"),
        },
        # longlist は OP3 が 20 件を点検する view なので、価値実現の経路を示す dated fact も
        # ここに置く。rank へは接続しない。
        "capital_control": {
            "tse_capital_policy_status": metrics.get("tse_capital_policy_status"),
            "tse_capital_policy_updated_on": metrics.get("tse_capital_policy_updated_on"),
            "large_holding_event_recent": metrics.get("large_holding_event_recent"),
            "large_holding_event_latest_on": metrics.get("large_holding_event_latest_on"),
            "tender_offer_event_recent": metrics.get("tender_offer_event_recent"),
            "tender_offer_event_latest_on": metrics.get("tender_offer_event_latest_on"),
        },
        "liquidity_status": "pass",
        "durability_warnings": list(string_sequence(durability_lens.get("caution_reasons"))),
        "event_warnings": [tag for tag in risk_tags if tag in _EVENT_RISK_TAGS],
        "selection_reasons": list(string_sequence(candidate.get("reason_tags"))),
        # opportunity thesis-scaffold は longlist から選ばれた銘柄も扱うため、
        # recommendation と同じ raw estimate + provenance contract を渡す。flat fields
        # は人間向け表示であり、転記時の正本にはしない。
        "estimate_snapshot": {
            "as_of": decision_input_seed.get("as_of"),
            **seed_estimates,
        },
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
    # FV の per-share basis は同じ artifact の raw close で証明する。価格 basis を証明できない
    # anchor は、自己株控除後 market cap と gross shares の混在を防ぐため判断面へ出さない。
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


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    durability_lens = _durability_lens_of(candidate)
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "selection_playbook": string_or_none(candidate.get("selection_playbook")),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "durability_rating": string_or_none(durability_lens.get("rating")),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_changed_summaries(
    base_by_ticker: Mapping[str, Mapping[str, object]],
    current_by_ticker: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    changed: list[dict[str, object]] = []
    for ticker in sorted(set(base_by_ticker) & set(current_by_ticker)):
        base = base_by_ticker[ticker]
        current = current_by_ticker[ticker]
        changed_fields = {
            key
            for key in (
                "durability_rating",
                "selection_playbook",
                "rank",
            )
            if base.get(key) != current.get(key)
        }
        if not changed_fields:
            continue
        changed.append(
            {
                "ticker": ticker,
                "changed_fields": sorted(changed_fields),
                "from": {key: base.get(key) for key in sorted(changed_fields)},
                "to": {key: current.get(key) for key in sorted(changed_fields)},
            }
        )
    return changed
