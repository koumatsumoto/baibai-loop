"""決算開示と as-of 財務のラグを判断面へ出す annotation。

決算ピーク週では「機械行の数字が直近の発表を含んでいるか」が判断の最初の分岐になる。
その問いに答えるのは 2 つの source の突き合わせである:

- ``jpx_earnings_calendar`` — 会社が公表した次回発表**予定**日。ticker あたり 1 行で、
  会社が予定日より前に開示しても行はそのまま残る。日付だけを見ても「これから」と
  「もう出た」が区別できない。
- ``jquants_fin_summaries`` — 実際に開示された数字。機械行の財務はここから作られる。

状態の判定はここへ集約し、表示層は転記する。ranking・gate・E[r] には入らない。

**過去時点の予定表を再現しない**: カレンダーは単一 snapshot で、取得時刻は coverage に
保存されるが、過去時点別の予定表を保持しない。historical backfill が現在の snapshot を
読む場合、その `next_earnings_status` と `stale_fin_flag` は当時の状態の証拠にはならない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from baibai_engine.market.jquants_models import JQuantsFinancialSummary

type NextEarningsStatus = Literal["scheduled_date_reached", "scheduled", "estimated", "unknown"]

# 財務行の日付と予定日が近ければ stale 判定を立てないための許容幅。
# 同じ予定の発表を確認した証拠にはせず、前倒し発表の推論にも使わない。
_SAME_EVENT_TOLERANCE_DAYS = 14

# 前年同期の発表日は決算期変更や休日繰り上げでずれる。これより離れた開示は同一四半期と
# みなさず、推定を出さない。
_ANCHOR_TOLERANCE_DAYS = 45


@dataclass(frozen=True, slots=True, kw_only=True)
class EarningsLag:
    """1 ticker 分の決算ラグ annotation。すべて ranking・gate へ入らない。"""

    latest_financial_disclosure_date: date | None
    next_earnings_estimated_date: date | None
    next_earnings_status: NextEarningsStatus
    # None = 判定材料が無い: 予定日が未来 / カレンダー行が無い / 行に財務が無い。
    # False は「財務開示日が予定日の14日前以降」であり、同じ決算の発表確認ではない。
    stale_fin_flag: bool | None


def estimate_next_announcement(
    summaries: Sequence[JQuantsFinancialSummary], *, asof: date
) -> date | None:
    """前年同期の「次の」発表日から次回発表日を推定する。

    ``jquants_fin_summaries`` には定期開示だけでなく業績予想修正・同一期の再開示が同じ
    粒度で入る。周期の刻みとして数えてよいのは会計期間が変わる行だけなので、
    ``period_end`` ごとに最初の開示だけを残してから 1 年前の同期を探す。

    材料が揃わなければ推定しない。欠落を推定で埋めない。
    """

    history = _disclosure_cycle(summaries, asof=asof)
    if not history:
        return None
    target = _shift_year(history[-1].disclosed_at, years=-1)
    anchor_index = min(
        range(len(history)),
        key=lambda index: abs((history[index].disclosed_at - target).days),
    )
    if abs((history[anchor_index].disclosed_at - target).days) > _ANCHOR_TOLERANCE_DAYS:
        return None
    if anchor_index + 1 >= len(history):
        return None
    estimated = _shift_year(history[anchor_index + 1].disclosed_at, years=1)
    return estimated if estimated > asof else None


def _disclosure_cycle(
    summaries: Sequence[JQuantsFinancialSummary], *, asof: date
) -> list[JQuantsFinancialSummary]:
    """発表 cadence に乗っている開示だけを、古い順で 1 期 1 行にして返す。

    ``jquants_fin_summaries`` には終了した期間の実績だけでなく、**まだ終わっていない期間**
    を指す来期ガイダンス行と、同一期の再開示・予想修正が同じ粒度で入る。cadence を刻む
    のは実績行だけなので、期末が開示日より後の行を落としてから ``period_end`` ごとに
    最初の 1 行を残す。ガイダンス行を残すと同じ期の実績行が捨てられ、周期が 1 つ飛ぶ。
    """

    ordered = sorted(
        (
            item
            for item in summaries
            if item.disclosed_at <= asof
            and item.period_end is not None
            and item.period_end <= item.disclosed_at
            and _carries_actuals(item)
        ),
        key=lambda item: item.disclosed_at,
    )
    cycle: list[JQuantsFinancialSummary] = []
    seen: set[date] = set()
    for item in ordered:
        assert item.period_end is not None  # filtered above
        if item.period_end in seen:
            continue
        seen.add(item.period_end)
        cycle.append(item)
    return cycle


def _carries_actuals(item: JQuantsFinancialSummary) -> bool:
    """実績を伴う開示か。

    業績予想・配当予想の修正は決算と同じ table へ入るが、実績列を持たない。cadence を
    刻むのは実績のほうなので、予想だけの行を周期の 1 つとして数えない。
    """

    return any(
        value is not None
        for value in (item.sales, item.cfo, item.profit, item.operating_profit, item.cumulative_eps)
    )


def _shift_year(value: date, *, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # 2/29 は前後の年に存在しない。周期推定にとって 1 日の差は意味がないので寄せる。
        return value.replace(year=value.year + years, day=28)


def build_earnings_lag(
    *,
    asof: date,
    latest_financial_disclosure_date: date | None,
    announcement_date: date | None,
    summaries: Sequence[JQuantsFinancialSummary],
) -> EarningsLag:
    """1 ticker の annotation を組む。

    ``latest_financial_disclosure_date`` は行の財務が読んだ開示日そのものを受け取る。
    ここで数え直すと「行が使った日」と「annotation が示す日」が静かにずれうるので、
    導出元は 1 つに保つ。

    状態と flag は予定日を共通入力にして決める。どちらも開示確認を表さない。
    """

    estimated = (
        None
        if announcement_date is not None and announcement_date > asof
        else estimate_next_announcement(summaries, asof=asof)
    )
    return EarningsLag(
        latest_financial_disclosure_date=latest_financial_disclosure_date,
        next_earnings_estimated_date=estimated,
        next_earnings_status=_status(
            asof=asof, announcement_date=announcement_date, estimated=estimated
        ),
        stale_fin_flag=_stale_fin_flag(
            asof=asof,
            announcement_date=announcement_date,
            latest_financial_disclosure_date=latest_financial_disclosure_date,
        ),
    )


def _status(
    *, asof: date, announcement_date: date | None, estimated: date | None
) -> NextEarningsStatus:
    """予定日と as-of の前後だけで決める。

    「予定日の手前に開示があるから前倒しで発表済み」という読みは試したが、実 store の
    retrospective で 89% が誤りだった。予定当日・経過後も日付だけでは開示を確認できない。
    ``scheduled_date_reached`` は予定表の時刻関係だけを表し、現実の発表を主張しない。
    """

    if announcement_date is not None:
        return "scheduled_date_reached" if announcement_date <= asof else "scheduled"
    return "estimated" if estimated is not None else "unknown"


def interpreted_next_earnings_status(saved_status: str | None) -> str | None:
    """Read a frozen legacy ``announced`` as the date-only claim it actually made.

    Saved run bytes remain immutable. This projection is for
    present-day readers and model inputs; historical exact reads retain the source.
    """

    return "scheduled_date_reached" if saved_status == "announced" else saved_status


def _stale_fin_flag(
    *, asof: date, announcement_date: date | None, latest_financial_disclosure_date: date | None
) -> bool | None:
    """予定日から離れた古い財務行か。発表の有無や原因は区別しない。

    立つのは「予定日が as-of 以前なのに、近い日付の財務行が無い」場合で、
    延期・決算期変更・provider 欠落のいずれでも立つ。どれであっても読み手のすべきことは
    同じ = 一次開示で切り分ける、なので、原因を断定しない。判定材料が無ければ ``None``。
    """

    if (
        announcement_date is None
        or announcement_date > asof
        or latest_financial_disclosure_date is None
    ):
        return None
    return (announcement_date - latest_financial_disclosure_date).days > _SAME_EVENT_TOLERANCE_DAYS


def tickers_without_calendar_rows(
    tickers: Sequence[str], calendar_tickers: Mapping[str, date]
) -> int:
    """カレンダー行を 1 つも持たない universe ticker 数。

    provider 側の欠落 = 件数の急増と、個別企業の未公表を区別するための計数で、閾値も
    判定も持たない。
    """

    return sum(1 for ticker in tickers if ticker not in calendar_tickers)


__all__ = [
    "EarningsLag",
    "NextEarningsStatus",
    "build_earnings_lag",
    "estimate_next_announcement",
    "interpreted_next_earnings_status",
    "tickers_without_calendar_rows",
]
