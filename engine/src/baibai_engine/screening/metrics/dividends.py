"""配当・自己株取得と株主還元を、予想と実績のbasisで計算する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.metrics.capital import (
    SHARE_COUNT_ANCHOR_TOLERANCE,
    _cumulative_adjustment_factor_after,
    _issued_matches_average_alias,
    _normalize_summaries_to_asof_basis,
    _shares_excluding_treasury,
)
from baibai_engine.screening.metrics.periods import _accounting_observation_key
from baibai_engine.screening.schema import (
    UNRESOLVED_DIVIDEND_BASIS,
)

# 通期実績 DPS の accrual 期間を bound する暦日窓。前期の通期実績開示行が窓内に
# 無い (実績が 1 期分しか無い) ときのフォールバックで、開示日から約 1 年遡って
# その期間内・開示前の分割を carry へ反映するために使う。
DIVIDEND_ACCRUAL_LOOKBACK_DAYS = 400

# 5 年 carry へ反復させる会社予想の上限。直近実績の 2 倍を超える進行期予想は、
# 特別配当か未実現の還元転換かを機械入力から区別できない。実績が正なら既存の実績経路へ
# 倒し、実績 0 / 未観測は初配当を消さないため予想を使う。
_DIVIDEND_FORECAST_SPIKE_MULTIPLE = 2.0


@dataclass(frozen=True, slots=True)
class ShareholderReturnChangeSignals:
    """Point-in-time facts used only by the calibration panel."""

    dps_streak_up: bool | None
    dps_yoy_latest: float | None
    dps_guidance_up: bool | None
    dividend_initiation: bool | None
    share_count_reduction_streak: int | None
    shareholder_return_change: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _DividendCarry:
    """carry 用に解決した配当利回りと、その基準・分割 factor の事実記録。"""

    dividend_yield: float | None
    dps_actual_annual: float | None
    dps_forecast_annual: float | None
    basis: str
    split_factor: float | None


@dataclass(frozen=True, slots=True)
class _DividendForecast:
    """One target-period annual dividend forecast and its source document."""

    target_period_end: date
    source: JQuantsFinancialSummary
    annual_dps: float | None


def _resolve_dividend_forecast(
    summaries: Sequence[JQuantsFinancialSummary],
) -> _DividendForecast | None:
    """Resolve annual DPS without carrying a forecast behind the latest actual DPS."""

    actual_dates = [row.disclosed_at for row in summaries if row.dps_actual_annual is not None]
    threshold = max(actual_dates, default=None)
    for source in sorted(summaries, key=lambda item: item.disclosed_at, reverse=True):
        if threshold is not None and source.disclosed_at < threshold:
            return None
        value = source.dps_forecast_annual
        if value is not None and value >= 0:
            return _DividendForecast(
                target_period_end=date.max,
                source=source,
                annual_dps=value,
            )
    return None


def _actual_dps_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in sorted(summaries, key=_accounting_observation_key, reverse=True)
        if summary.dps_actual_annual is not None
    ]


# 配当の基準日と corporate action がこの日数以内に並ぶ年度は換算しない。日本の分割は
# 「権利落ち = 基準日の前営業日、効力発生 = 基準日の翌日」が定型なので、期末配当の基準日と
# 分割の権利落ち日が数日違いで並ぶ。store が持つのは権利落ち日だけで効力発生日を持たない
# ため、その配当が action の前の株数で払われたのか後なのかを言えない。実測では調整日は
# 基準日の 5 日以内に集中し、5〜20 日の帯には 1 件も現れない (#901)。
DIVIDEND_RECORD_DATE_GUARD_DAYS = 5

# 総額から出した 1 株当たりと、支払ごとに換算した合計が食い違ってよい幅。総額は百万円
# 単位で開示され、割る株数は期末時点なので、支払の基準日の株数とは自社株買いのぶんだけ
# ずれる。この幅を超える食い違いは、どちらかの経路が別の株式基準を見ている合図になる。
DIVIDEND_ROUTE_TOLERANCE = 0.05


def _dividend_accrual_start(row: JQuantsFinancialSummary) -> date:
    """その年度の配当が積み上がり始めた日。

    当期会計期間の開始日を使う。前期の通期実績開示日を起点にすると、同一年度の訂正開示が
    直前に来た行で窓が数日へ縮み、窓内の corporate action が消える。
    """
    if row.period_start is not None:
        return row.period_start
    if row.fiscal_year_end is not None:
        return add_months_clamped(row.fiscal_year_end, -12)
    return row.disclosed_at - timedelta(days=DIVIDEND_ACCRUAL_LOOKBACK_DAYS)


def _dividend_basis_factor(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    accrual_start: date,
    disclosed_at: date,
) -> float:
    """会計期間に起きた分割・併合の累積 factor。1.0 なら年度を通じて基準が一意。

    年間 DPS は中間・期末それぞれの基準日時点の株式基準で記載され、開示日で基準が決まる
    わけではない。期間内に分割・併合が入ると支払ごとに action の前後が分かれるので、
    報告された年間値をそのまま株価と比べられない。
    """
    return _cumulative_adjustment_factor_after(ticker_bars, accrual_start, disclosed_at)


def _dividend_record_dates(row: JQuantsFinancialSummary) -> tuple[tuple[float | None, date], ...]:
    """支払ごとの 1 株当たり配当と、その基準日。基準日は四半期末に置く。"""
    fiscal_year_end = row.fiscal_year_end
    if fiscal_year_end is None:
        return ()
    return (
        (row.dividend_q1, add_months_clamped(fiscal_year_end, -9)),
        (row.dividend_interim, add_months_clamped(fiscal_year_end, -6)),
        (row.dividend_q3, add_months_clamped(fiscal_year_end, -3)),
        (row.dividend_year_end, fiscal_year_end),
    )


def _shares_for_per_share(row: JQuantsFinancialSummary) -> float | None:
    """円の総額を 1 株当たりへ直すのに使える株数。壊れている行は答えない。"""
    shares = _shares_excluding_treasury(row.shares_outstanding, row.treasury_shares)
    if shares is None:
        return None
    anchor = row.average_shares
    if anchor is None or anchor <= 0:
        return shares
    ratio = shares / anchor
    if not 1 / SHARE_COUNT_ANCHOR_TOLERANCE <= ratio <= SHARE_COUNT_ANCHOR_TOLERANCE:
        return None
    return shares


def _asof_basis_dividend(
    row: JQuantsFinancialSummary,
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    asof_date: date,
) -> float | None:
    """分割・併合を跨いだ年度の年間配当を asof の株式基準で答える。答えられなければ None。

    支払ごとに、その基準日より後の調整だけを掛けて足す。この経路は期末発行済株式数を
    使わないので、提出者がその株数を遡及修正したかどうかに左右されない。基準日のすぐ
    そばに調整がある年度は、権利落ち日しか持たない store からは前後を決められないので
    答えない。答えられた値は 2 つの独立な量と突き合わせる。調整を掛ける前の明細合計が
    報告された年間値と一致すること (明細の欠落を「解決済み」として出さない)、そして
    株式基準を持たない配当総額から出した 1 株当たりと一致すること。
    """
    reported = row.dps_actual_annual
    if reported is None:
        return None
    # 無配の年度に確定すべき株式基準は無い。0 円は何倍しても 0 円なので、期間内に
    # 調整があっても答えは 0 で確定する。ここを拒否に倒すと、分割を出す無配銘柄が
    # 利回りだけでなく E[r] ごと判断面から消える。
    if reported == 0:
        return 0.0
    payments = _dividend_record_dates(row)
    if not payments or all(value is None for value, _ in payments):
        return None
    # 明細は feed の欠落で一部だけ来ることがある。調整前の合計が報告年間値と合わない行は
    # 真値の一部しか持っていないので、換算しても真値の一部にしかならない。
    #
    # 2 つの量は株式基準が違う。明細は開示されたままで、`dps_actual_annual` は
    # `_normalize_summaries_to_asof_basis` が asof 基準へ寄せている。同じ換算を明細側へ
    # 掛けてから比べる。境界も揃える — 片方だけが換算する、あるいは片方だけが開示日当日の
    # 権利落ちを数えると、比は必ず換算係数の逆数になり、その年度を「明細が欠けている」と
    # して捨てる。捨てた年度は増配判定ごと消える。
    detail_sum = sum(value or 0.0 for value, _ in payments) * _cumulative_adjustment_factor_after(
        ticker_bars, row.disclosed_at, asof_date, include_boundary_day=True
    )
    if abs(detail_sum / reported - 1.0) > DIVIDEND_ROUTE_TOLERANCE:
        return None
    window_start = _dividend_accrual_start(row)
    adjustments = [
        bar
        for bar in ticker_bars
        if window_start < bar.traded_at <= row.disclosed_at
        and bar.adjustment_factor not in (None, 0.0, 1.0)
    ]
    guard = timedelta(days=DIVIDEND_RECORD_DATE_GUARD_DAYS)
    for value, record_date in payments:
        if not value:
            continue
        if any(abs(bar.traded_at - record_date) <= guard for bar in adjustments):
            return None
    resolved = sum(
        (value or 0.0) * _cumulative_adjustment_factor_after(ticker_bars, record_date, asof_date)
        for value, record_date in payments
    )
    if resolved <= 0:
        return None
    amount = row.dividend_total_annual
    shares = _shares_for_per_share(row)
    if (
        amount is not None
        and amount > 0
        and shares is not None
        and abs((amount / shares) / resolved - 1.0) > DIVIDEND_ROUTE_TOLERANCE
    ):
        return None
    return resolved


def _resolve_dividend_carry(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    latest_price: float,
    asof_date: date,
) -> _DividendCarry:
    """carry 用の配当利回りと基準を解決する。

    実績 DPS は開示時点の株式基準で記載され、`_normalize_summaries_to_asof_basis` が
    開示日より後の分割を掛けて asof 基準へ寄せている。残るのは会計期間の中で起きた
    分割・併合で、報告された年間値は支払ごとに基準が分かれるためそのままでは株価と
    比べられない。その年度は支払ごとに換算し直し (`_asof_basis_dividend`)、換算できな
    ければ実績側の利回りを出さない。carry の配当側は予想 DPS または基準を解決できた実績
    DPS だけで組む。分割を跨ぐ行の正の予想 DPS は基準不明として落とし、明示 0 は残す。

    予想は実績より優先するが、優先できるのは最新の実績観測と同日以降で、かつ正の実績 DPS の 2 倍
    以下のときだけである。2 倍を超える跳ねは特別配当か未実現の還元転換かを機械入力から
    区別できないので、5 年反復する carry には実績を使う。会社が予想を取り下げた後も過去の
    予想を引き当て続けると、無配化した会社に当時の配当額の利回りが付き、E[r] の reversion
    上限 (5%/年) を単独で超える carry を作る。
    """
    actual_rows = _actual_dps_rows(summaries)
    forecast_state = _resolve_dividend_forecast(summaries)
    forecast = forecast_state.annual_dps if forecast_state is not None else None
    basis_factor = 1.0
    actual_annual: float | None = None
    if actual_rows:
        latest_actual = actual_rows[0]
        assert latest_actual.dps_actual_annual is not None
        basis_factor = _dividend_basis_factor(
            ticker_bars,
            accrual_start=_dividend_accrual_start(latest_actual),
            disclosed_at=latest_actual.disclosed_at,
        )
        if basis_factor == 1.0:
            actual_annual = latest_actual.dps_actual_annual
        else:
            actual_annual = _asof_basis_dividend(latest_actual, ticker_bars, asof_date=asof_date)

    recorded_factor = basis_factor if basis_factor != 1.0 else None

    forecast_is_usable_for_carry = (
        forecast is not None
        and forecast >= 0
        and (
            actual_annual is None
            or actual_annual <= 0
            or forecast <= _DIVIDEND_FORECAST_SPIKE_MULTIPLE * actual_annual
        )
    )

    if latest_price <= 0:
        basis = "unavailable"
    elif forecast_is_usable_for_carry and forecast is not None:
        return _DividendCarry(
            dividend_yield=forecast / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis="forecast_annual",
            split_factor=recorded_factor,
        )
    elif actual_annual is not None and actual_annual >= 0:
        return _DividendCarry(
            dividend_yield=actual_annual / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis=(
                "actual_reported"
                if actual_annual == 0 or recorded_factor is None
                else "actual_record_date_resolved"
            ),
            split_factor=recorded_factor,
        )
    elif recorded_factor is not None and actual_annual is None:
        # 正の実績が解決できなかった年度は無配と区別する。
        basis = UNRESOLVED_DIVIDEND_BASIS
    else:
        basis = "unavailable"

    return _DividendCarry(
        dividend_yield=None,
        dps_actual_annual=actual_annual,
        dps_forecast_annual=forecast,
        basis=basis,
        split_factor=recorded_factor,
    )


def build_shareholder_return_change_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> ShareholderReturnChangeSignals:
    """Build preregistered shareholder-return change facts for calibration.

    Revisions are resolved before field availability is inspected: a null in the
    newest revision cannot silently fall back to an older value. DPS and gross
    share counts are normalized to the cohort's stock basis, while the latter is
    deliberately described as share-count reduction rather than proof of a
    buyback because the source includes treasury stock.
    """
    available = sorted(
        (summary for summary in summaries if summary.disclosed_at <= asof_date),
        key=lambda item: item.disclosed_at,
    )
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)

    dps_values = _asof_basis_fy_dps(fy_rows, ticker_bars, asof_date=asof_date)
    latest_pair = _latest_consecutive_values(fy_rows, dps_values, count=2)
    latest_three = _latest_consecutive_values(fy_rows, dps_values, count=3)

    dps_streak_up = (
        latest_three[0] <= latest_three[1] <= latest_three[2] if latest_three is not None else None
    )
    actual_up: bool | None = None
    dps_yoy_latest: float | None = None
    if latest_pair is not None:
        prior_dps, latest_dps = latest_pair
        actual_up = latest_dps > prior_dps
        if prior_dps > 0:
            dps_yoy_latest = (latest_dps / prior_dps) - 1.0
        elif latest_dps == 0:
            dps_yoy_latest = 0.0

    latest_actual_row = _latest_row_with_value(fy_rows, dps_values)
    if latest_actual_row is not None:
        assert latest_actual_row.fiscal_year_end is not None
        latest_actual = dps_values[latest_actual_row.fiscal_year_end]
    else:
        latest_actual = None
    forecast_state = _resolve_dividend_forecast(normalized)
    forecast = forecast_state.annual_dps if forecast_state is not None else None
    dps_guidance_up = (
        forecast > latest_actual if forecast is not None and latest_actual is not None else None
    )
    dividend_initiation = _dividend_initiation(
        latest_pair=latest_pair,
        latest_actual=latest_actual,
        forecast=forecast,
    )

    share_values = {
        row.fiscal_year_end: row.shares_outstanding
        for row in fy_rows
        if row.fiscal_year_end is not None
        and row.shares_outstanding is not None
        and row.shares_outstanding > 0
        and not (
            row.treasury_shares is not None
            and _issued_matches_average_alias(row, row.treasury_shares, normalized)
        )
    }
    latest_share_three = _latest_consecutive_values(fy_rows, share_values, count=3)
    share_count_reduction_streak: int | None = None
    if latest_share_three is not None:
        share_count_reduction_streak = 0
        for prior, current in reversed(tuple(pairwise(latest_share_three))):
            if current >= prior:
                break
            share_count_reduction_streak += 1

    change_components = (
        actual_up,
        dps_guidance_up,
        dividend_initiation,
        (share_count_reduction_streak >= 1 if share_count_reduction_streak is not None else None),
    )
    if any(value is True for value in change_components):
        shareholder_return_change: bool | None = True
    elif all(value is False for value in change_components):
        shareholder_return_change = False
    else:
        shareholder_return_change = None

    return ShareholderReturnChangeSignals(
        dps_streak_up=dps_streak_up,
        dps_yoy_latest=dps_yoy_latest,
        dps_guidance_up=dps_guidance_up,
        dividend_initiation=dividend_initiation,
        share_count_reduction_streak=share_count_reduction_streak,
        shareholder_return_change=shareholder_return_change,
    )


def _latest_fy_revisions(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    latest: dict[date, JQuantsFinancialSummary] = {}
    for summary in summaries:
        if summary.fiscal_period != "FY" or summary.fiscal_year_end is None:
            continue
        previous = latest.get(summary.fiscal_year_end)
        if previous is None or summary.disclosed_at > previous.disclosed_at:
            latest[summary.fiscal_year_end] = summary
    return [latest[fiscal_year_end] for fiscal_year_end in sorted(latest)]


def _asof_basis_fy_dps(
    fy_rows: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    asof_date: date,
) -> dict[date, float]:
    """通期実績 DPS を fiscal year end で引けるようにする。

    行は `_normalize_summaries_to_asof_basis` を通っているので asof 基準に揃っている。
    会計期間に分割・併合が入った年度だけは報告値の基準が支払ごとに分かれるので、支払
    ごとに換算し直す。換算できない年度は落とし、落ちた年度を含む YoY と streak は
    `_latest_consecutive_values` が None を返して増配と減配を取り違えない。
    """
    values: dict[date, float] = {}
    for row in fy_rows:
        fiscal_year_end = row.fiscal_year_end
        value = row.dps_actual_annual
        if fiscal_year_end is None or value is None or value < 0:
            continue
        factor = _dividend_basis_factor(
            ticker_bars,
            accrual_start=_dividend_accrual_start(row),
            disclosed_at=row.disclosed_at,
        )
        if factor == 1.0:
            values[fiscal_year_end] = value
            continue
        resolved = _asof_basis_dividend(row, ticker_bars, asof_date=asof_date)
        if resolved is not None:
            values[fiscal_year_end] = resolved
    return values


def _latest_consecutive_values(
    fy_rows: Sequence[JQuantsFinancialSummary],
    values: Mapping[date, float],
    *,
    count: int,
) -> tuple[float, ...] | None:
    if len(fy_rows) < count:
        return None
    selected = list(fy_rows[-count:])
    for prior, current in pairwise(selected):
        if current.fiscal_year_end is None or prior.fiscal_year_end is None:
            return None
        if add_months_clamped(current.fiscal_year_end, -12) != prior.fiscal_year_end:
            return None
    resolved: list[float] = []
    for row in selected:
        if row.fiscal_year_end is None or row.fiscal_year_end not in values:
            return None
        resolved.append(values[row.fiscal_year_end])
    return tuple(resolved)


def _latest_row_with_value(
    fy_rows: Sequence[JQuantsFinancialSummary], values: Mapping[date, float]
) -> JQuantsFinancialSummary | None:
    if not fy_rows:
        return None
    row = fy_rows[-1]
    return row if row.fiscal_year_end is not None and row.fiscal_year_end in values else None


def _dividend_initiation(
    *,
    latest_pair: tuple[float, ...] | None,
    latest_actual: float | None,
    forecast: float | None,
) -> bool | None:
    if latest_pair is not None:
        prior, latest = latest_pair
        if prior == 0 and latest > 0:
            return True
        if latest > 0:
            return False
    if latest_actual == 0 and forecast is not None:
        return forecast > 0
    return None
