"""会計期間を整列し、TTMと前年比較の対象を選ぶ。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import TTMRules
from baibai_engine.screening.schema import (
    TTMQuality,
)

_AccountingObservationKey = tuple[date, int, date, date, int, date]

_FISCAL_PERIOD_ORDER = {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}


def _accounting_observation_key(
    summary: JQuantsFinancialSummary,
) -> _AccountingObservationKey:
    """Period-first order for actual facts and capital state.

    A delayed correction may be disclosed after the current quarter while describing
    an older period.  A valid completed-period end therefore precedes accounting labels
    and source revision order in this key.  A future reported period uses only its
    period-start lower bound; an inverted period has no chronology authority.  This
    preserves a lone future-labelled actual observation, prevents a malformed Q1 from
    replacing a valid Q2, and lets a completed short fiscal year outrank an earlier
    quarter whose nominal fiscal-year end happens to be later.
    """

    reported_end = summary.period_end or summary.fiscal_year_end
    completed = (
        reported_end
        if reported_end is not None
        and reported_end <= summary.disclosed_at
        and (summary.period_start is None or summary.period_start <= reported_end)
        else None
    )
    chronology = completed or (
        summary.period_start
        if reported_end is not None
        and reported_end > summary.disclosed_at
        and summary.period_start is not None
        else date.min
    )
    period_order = _FISCAL_PERIOD_ORDER.get(summary.fiscal_period or "", 0)
    return (
        chronology,
        int(completed is not None),
        summary.fiscal_year_end or date.min,
        summary.period_start or date.min,
        period_order,
        summary.disclosed_at,
    )


_ACTUAL_ANCHOR_FIELDS = (
    "sales",
    "cfo",
    "cash_eq",
    "total_assets",
    "equity",
    "operating_profit",
    "ordinary_profit",
    "profit",
    "eps_ttm",
    "bps",
    "shares_outstanding",
    "treasury_shares",
    "equity_to_asset_ratio",
    "dps_actual_annual",
    "average_shares",
)


def _actual_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in summaries
        if any(getattr(summary, field_name) is not None for field_name in _ACTUAL_ANCHOR_FIELDS)
    ]


def _latest_actual_row(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    field_names: Sequence[str] = (),
) -> JQuantsFinancialSummary | None:
    required = tuple(field_names) or _ACTUAL_ANCHOR_FIELDS
    candidates = [
        summary
        for summary in summaries
        if (
            all(getattr(summary, field_name) is not None for field_name in required)
            if field_names
            else any(getattr(summary, field_name) is not None for field_name in required)
        )
    ]
    return max(candidates, key=_accounting_observation_key, default=None)


def _latest_actual_row_with_any(
    summaries: Sequence[JQuantsFinancialSummary],
    field_names: Sequence[str],
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if any(getattr(summary, field_name) is not None for field_name in field_names)
    ]
    return max(candidates, key=_accounting_observation_key, default=None)


def _latest_actual_row_by_field_priority(
    summaries: Sequence[JQuantsFinancialSummary],
    field_names: Sequence[str],
) -> JQuantsFinancialSummary | None:
    """Pick the preferred non-null metric without leaving the latest accounting period.

    Actual corrections may update only a subset of a statement.  The newest row for a
    period therefore cannot make a more specific metric from an earlier revision of the
    same period disappear, but a metric from an older period must not replace a less
    specific metric observed in the current period.
    """

    latest = _latest_actual_row_with_any(summaries, field_names)
    if latest is None:
        return None
    period_key = _accounting_observation_key(latest)[:-1]
    for field_name in field_names:
        candidates = [
            summary
            for summary in summaries
            if _accounting_observation_key(summary)[:-1] == period_key
            and getattr(summary, field_name) is not None
        ]
        if candidates:
            return max(candidates, key=_accounting_observation_key)
    return None


def _latest_non_null_row(
    summaries: Sequence[JQuantsFinancialSummary], field_name: str
) -> JQuantsFinancialSummary | None:
    """指定 field を観測した最新行を返す。値だけでなく資本状態の出所行を保持する。"""

    return _latest_actual_row(summaries, field_names=(field_name,))


def _latest_complete_row(
    summaries: Sequence[JQuantsFinancialSummary], field_names: Sequence[str]
) -> JQuantsFinancialSummary | None:
    """指定した値を同時に観測した最新行を返す。"""

    return _latest_actual_row(summaries, field_names=field_names)


# 1 株当たりで開示される field。期をまたぐ和・差を作れないので TTM 合成へ渡さない。
_PER_SHARE_FIELDS = frozenset(
    {
        "eps_ttm",
        "forecast_eps",
        "bps",
        "dps_actual_annual",
        "dps_forecast_annual",
        "dividend_q1",
        "dividend_interim",
        "dividend_q3",
        "dividend_year_end",
    }
)


def _carry_forward(
    summaries: Sequence[JQuantsFinancialSummary],
    field_name: str,
    latest: JQuantsFinancialSummary | None,
) -> tuple[float | None, int | None]:
    """直近の非 null 行から値を取り、latest 行からの遅延日数を添える。

    lag 0 = latest 行自身が値を持つ。lag > 0 = carry-forward された staleness。
    値が窓内のどの行にも無ければ (None, None)。
    """
    if latest is None:
        return None, None
    source = _latest_actual_row(summaries, field_names=(field_name,))
    if source is None:
        return None, None
    value = getattr(source, field_name)
    assert value is not None
    lag = max(0, (latest.disclosed_at - source.disclosed_at).days)
    return float(value), lag


def _latest_summary(summaries: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    return summaries[-1] if summaries else None


def _prior_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
) -> JQuantsFinancialSummary | None:
    """Return the same fiscal period in the previous fiscal year.

    Assumes summaries are ordered oldest-first; revisions of the same fiscal
    period are resolved by taking the most recent occurrence.
    """
    latest = (
        _latest_actual_row(summaries, field_names=(field_name,))
        if field_name is not None
        else _latest_summary(summaries)
    )
    if latest is None:
        return None
    if latest.period_start is not None and latest.period_end is not None:
        matched = _matched_prior_period_summary(
            summaries,
            latest,
            ttm_rules,
            field_name=field_name,
        )
        if matched is not None:
            return matched
    if latest.fiscal_period is None or latest.fiscal_year_end is None:
        return None
    target_fiscal_year_end = add_months_clamped(latest.fiscal_year_end, -12)
    candidates = [
        summary
        for summary in _actual_rows(summaries)
        if field_name is None or getattr(summary, field_name) is not None
        if summary.fiscal_period == latest.fiscal_period
        and summary.fiscal_year_end == target_fiscal_year_end
    ]
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _ttm_value(
    summaries: Sequence[JQuantsFinancialSummary],
    field: str,
    ttm_rules: TTMRules,
) -> tuple[float | None, TTMQuality]:
    """`直近累計 + 前期通期 - 前年同期間累計` で 12 か月へ直す。

    この合成は各項が同じ単位で加減できることを前提にする。円の総額は満たすが、1 株当たり
    の値は各項が自分の期の株数で割られているので満たさない。株数が動いた会社では黒字が
    赤字に見える。per-share の field を渡すのは呼び出し側の誤りなので受け付けない。
    """
    if field in _PER_SHARE_FIELDS:
        raise ValueError(f"{field} is per share; compose the yen line and convert once at the end")
    # 非実績開示は過去の実績を取り消さない。必要fieldの欠損は選択後に判定する。
    actuals = _actual_rows(summaries)
    latest = _latest_actual_row(actuals)
    if (
        latest is None
        or getattr(latest, field) is None
        or latest.period_start is None
        or latest.period_end is None
    ):
        return None, TTMQuality.UNAVAILABLE
    latest_value = getattr(latest, field)
    if latest_value is None:
        return None, TTMQuality.UNAVAILABLE
    latest_days = _period_days(latest)
    if latest_days is None:
        return None, TTMQuality.UNAVAILABLE
    if ttm_rules.full_year_min_days <= latest_days <= ttm_rules.full_year_max_days:
        return latest_value, TTMQuality.EXACT
    if latest.fiscal_year_end is None:
        return None, TTMQuality.UNAVAILABLE
    prior_fy_end = add_months_clamped(latest.fiscal_year_end, -12)
    prior_fy = _latest_full_year_summary(actuals, prior_fy_end, ttm_rules)
    prior_same = _matched_prior_period_summary(
        actuals,
        latest,
        ttm_rules,
    )
    if prior_fy is None or prior_same is None:
        return None, TTMQuality.UNAVAILABLE
    prior_fy_value = getattr(prior_fy, field)
    prior_same_value = getattr(prior_same, field)
    if prior_fy_value is None or prior_same_value is None:
        return None, TTMQuality.UNAVAILABLE
    return latest_value + prior_fy_value - prior_same_value, TTMQuality.EXACT


def _latest_full_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    fiscal_year_end: date,
    ttm_rules: TTMRules,
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if summary.fiscal_year_end == fiscal_year_end
        and (days := _period_days(summary)) is not None
        and ttm_rules.full_year_min_days <= days <= ttm_rules.full_year_max_days
    ]
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _matched_prior_period_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    latest: JQuantsFinancialSummary,
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
) -> JQuantsFinancialSummary | None:
    if latest.period_start is None or latest.period_end is None:
        return None
    latest_days = _period_days(latest)
    prior_start = add_months_clamped(latest.period_start, -12)
    prior_end = add_months_clamped(latest.period_end, -12)
    if latest_days is None:
        return None
    max_length_delta = max(1, round(latest_days * ttm_rules.period_length_tolerance_ratio))
    candidates: list[JQuantsFinancialSummary] = []
    for summary in _actual_rows(summaries):
        if field_name is not None and getattr(summary, field_name) is None:
            continue
        if summary.period_start is None or summary.period_end is None:
            continue
        summary_days = _period_days(summary)
        if summary_days is None or abs(summary_days - latest_days) > max_length_delta:
            continue
        if abs((summary.period_end - prior_end).days) > ttm_rules.period_end_tolerance_days:
            continue
        if abs((summary.period_start - prior_start).days) > ttm_rules.period_end_tolerance_days:
            continue
        candidates.append(summary)
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _period_days(summary: JQuantsFinancialSummary) -> int | None:
    if summary.period_start is None or summary.period_end is None:
        return None
    days = (summary.period_end - summary.period_start).days + 1
    return days if days > 0 else None
