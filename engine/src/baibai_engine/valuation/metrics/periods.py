"""会計期間を整列し、TTMと前年比較の対象を選ぶ。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.financial_documents import (
    AccountingPeriodKey,
    accounting_period_identity,
    disclosure_revision,
    latest_disclosure_candidates,
    statement_basis,
)
from baibai_engine.market.financial_periods import (
    ACTUAL_FIELDS as _ACTUAL_ANCHOR_FIELDS,
    compose_ttm_amount,
    period_days as _period_days,
    prior_year_period_matches,
    resolve_actual_conflicts as resolve_actual_conflicts,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.metric_quality import TTMQuality
from baibai_engine.valuation.metrics.models import TTMRules

_AccountingObservationKey = tuple[AccountingPeriodKey, tuple[date, str]]


def _accounting_observation_key(summary: JQuantsFinancialSummary) -> _AccountingObservationKey:
    return accounting_period_identity(summary), disclosure_revision(summary)


def _latest_consistent_actual(
    candidates: Sequence[JQuantsFinancialSummary], fields: Sequence[str]
) -> JQuantsFinancialSummary | None:
    if not candidates:
        return None
    period = max(map(accounting_period_identity, candidates))
    latest = latest_disclosure_candidates(
        [row for row in candidates if accounting_period_identity(row) == period]
    )
    for field in fields:
        if any(field in row.unresolved_actual_fields for row in latest):
            return None
        values = {getattr(row, field) for row in latest if getattr(row, field) is not None}
        if len(values) > 1:
            return None
    # Availability chooses a representative only after numeric agreement. It does
    # not establish a publication order, and never fills a capital pair across rows.
    return max(
        latest,
        key=lambda row: sum(
            getattr(row, field) is not None for field in (fields or _ACTUAL_ANCHOR_FIELDS)
        ),
    )



def _actual_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in summaries
        if summary.unresolved_actual_fields
        or any(getattr(summary, field_name) is not None for field_name in _ACTUAL_ANCHOR_FIELDS)
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
            all(
                getattr(summary, field_name) is not None
                or field_name in summary.unresolved_actual_fields
                for field_name in required
            )
            if field_names
            else bool(summary.unresolved_actual_fields)
            or any(getattr(summary, field_name) is not None for field_name in required)
        )
    ]
    return _latest_consistent_actual(candidates, required if field_names else ())


def _latest_actual_row_with_any(
    summaries: Sequence[JQuantsFinancialSummary],
    field_names: Sequence[str],
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if any(
            getattr(summary, field_name) is not None
            or field_name in summary.unresolved_actual_fields
            for field_name in field_names
        )
    ]
    return _latest_consistent_actual(candidates, ())


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
    period_key = accounting_period_identity(latest)
    for field_name in field_names:
        candidates = [
            summary
            for summary in summaries
            if accounting_period_identity(summary) == period_key
            and (
                getattr(summary, field_name) is not None
                or field_name in summary.unresolved_actual_fields
            )
        ]
        if candidates:
            return _latest_consistent_actual(candidates, (field_name,))
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
    # Native accounting dates own comparability; FY/Q labels cannot replace them.
    prior = _matched_prior_period_summary(
        summaries,
        latest,
        ttm_rules,
        field_name=field_name,
    )
    # Issued shares describe the issuer, not its consolidated reporting scope.
    # Financial YoY must not compare individual amounts with group amounts, and
    # a mismatching latest revision must not revive an older convenient basis.
    if (
        prior is not None
        and field_name != "shares_outstanding"
        and statement_basis(prior) != statement_basis(latest)
    ):
        return None
    return prior


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
    anchor = _latest_actual_row(actuals)
    cohort = (
        latest_disclosure_candidates(
            [
                row
                for row in actuals
                if accounting_period_identity(row) == accounting_period_identity(anchor)
            ]
        )
        if anchor is not None
        else []
    )
    latest = _latest_actual_row(cohort, field_names=(field,))
    if latest is None or latest.period_start is None or latest.period_end is None:
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
    prior_fy = _latest_full_year_summary(actuals, prior_fy_end, ttm_rules, field_name=field)
    prior_same = _matched_prior_period_summary(
        actuals,
        latest,
        ttm_rules,
        value_field=field,
    )
    if prior_fy is None or prior_same is None:
        return None, TTMQuality.UNAVAILABLE
    # A fiscal-date match cannot make individual and consolidated amounts additive.
    # Keep each reported operand intact rather than searching older revisions for
    # a convenient basis. Matching labels alone do not prove unchanged group scope.
    if len({statement_basis(row) for row in (latest, prior_fy, prior_same)}) != 1:
        return None, TTMQuality.UNAVAILABLE
    prior_fy_value = getattr(prior_fy, field)
    prior_same_value = getattr(prior_same, field)
    if prior_fy_value is None or prior_same_value is None:
        return None, TTMQuality.UNAVAILABLE
    return compose_ttm_amount(latest_value, prior_fy_value, prior_same_value), TTMQuality.EXACT


def _latest_full_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    fiscal_year_end: date,
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if summary.fiscal_year_end == fiscal_year_end
        and (days := _period_days(summary)) is not None
        and ttm_rules.full_year_min_days <= days <= ttm_rules.full_year_max_days
    ]
    return _latest_consistent_actual(candidates, (field_name,) if field_name else ())


def _matched_prior_period_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    latest: JQuantsFinancialSummary,
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
    value_field: str | None = None,
) -> JQuantsFinancialSummary | None:
    if latest.period_start is None or latest.period_end is None:
        return None
    latest_days = _period_days(latest)
    if latest_days is None:
        return None
    candidates: list[JQuantsFinancialSummary] = []
    for summary in _actual_rows(summaries):
        if (
            field_name is not None
            and getattr(summary, field_name) is None
            and field_name not in summary.unresolved_actual_fields
        ):
            continue
        if summary.period_start is None or summary.period_end is None:
            continue
        if not prior_year_period_matches(
            summary,
            latest,
            length_tolerance_ratio=ttm_rules.period_length_tolerance_ratio,
            end_tolerance_days=ttm_rules.period_end_tolerance_days,
        ):
            continue
        candidates.append(summary)
    comparison_field = value_field or field_name
    return _latest_consistent_actual(
        candidates, (comparison_field,) if comparison_field is not None else ()
    )


