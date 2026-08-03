"""決算開示と as-of 財務のラグを判断面へ出すための annotation。

決算ピーク週では「機械行の数字が直近の発表を含んでいるか」が判断の最初の分岐になる。
その問いに答えるのは 2 つの source の突き合わせである:

- ``jquants_earnings_calendar`` — 会社が公表した次回発表予定日。ticker あたり 1 行しか
  持たないので、発表当日はその行が「今日」を指したまま残り、予定と発表済みが同じ
  日付として見える。
- ``jquants_fin_summaries`` — 実際に開示された数字。機械行の財務はここから作られる。

発表があってから summary が取り込まれるまでには窓があり、その間 candidate の財務・FV
アンカー・E[r] は旧四半期のままになる。ここで作るのは ranking にも gate にも入らない
annotation で、その窓に居ることを読み手へ渡すだけである。

カレンダー行が無い ticker は実測 16.3% あり、「未公表」と「データ欠落」が区別できない。
過去の開示周期から次回を推定して estimate として併記し、確定日と混ぜない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .providers.jquants import JQuantsFinancialSummary


class CalendarEntry(Protocol):
    """発表予定カレンダーの 1 行。ticker と発表日だけを読む。"""

    @property
    def ticker(self) -> str: ...

    @property
    def announcement_date(self) -> date: ...


@dataclass(frozen=True, slots=True)
class EarningsLag:
    """1 ticker 分の決算ラグ annotation。すべて ranking・gate へ入らない。"""

    fin_latest_disclosed_date: date | None
    next_earnings_estimated_date: date | None
    stale_fin_flag: bool


def latest_disclosed_dates(
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]], *, asof: date
) -> dict[str, date]:
    """as-of 時点で store が持つ最新開示日を ticker ごとに返す。

    未来日の開示は point-in-time 再構成を壊すので除く。
    """

    latest: dict[str, date] = {}
    for ticker, summaries in summaries_by_ticker.items():
        dates = [item.disclosed_at for item in summaries if item.disclosed_at <= asof]
        if dates:
            latest[ticker] = max(dates)
    return latest


def index_calendar_announcements(
    entries: Sequence[CalendarEntry], *, asof: date
) -> dict[str, date]:
    """ticker ごとに、as-of から見て判断に効くカレンダー行を 1 つ選ぶ。

    カレンダーは ticker あたり 1 行しか持たないので通常は選択の余地が無い。複数行ある
    場合は **as-of 以前の最新**を優先する。数字が追いついたかを問えるのは既に起きた
    発表だけで、未来の予定日は staleness を判定しない。
    """

    past: dict[str, date] = {}
    future: dict[str, date] = {}
    for entry in entries:
        if entry.announcement_date <= asof:
            current = past.get(entry.ticker)
            if current is None or entry.announcement_date > current:
                past[entry.ticker] = entry.announcement_date
        else:
            upcoming = future.get(entry.ticker)
            if upcoming is None or entry.announcement_date < upcoming:
                future[entry.ticker] = entry.announcement_date
    return {**future, **past}


def estimate_next_announcement(
    summaries: Sequence[JQuantsFinancialSummary], *, asof: date
) -> date | None:
    """前年同四半期の次回発表日から次の発表日を推定する。

    直近開示の 1 年前にあたる開示を探し、その **次** の開示日を 1 年ずらす。四半期
    周期は年ごとにほぼ固定なので、周期そのものを仮定するより実績を写すほうが外れにくい。
    材料が揃わなければ推定しない。欠落を推定で埋めない。
    """

    history = sorted(
        (item for item in summaries if item.disclosed_at <= asof),
        key=lambda item: item.disclosed_at,
    )
    if not history:
        return None
    latest = history[-1]
    target = _shift_year(latest.disclosed_at)
    if target is None:
        return None
    # 直近開示の 1 年前に最も近い開示を起点にする。同日が無くても周期は年ごとに
    # 数日ずれる程度なので、最近傍を取れば同一四半期を指す。
    anchor_index = min(
        range(len(history)),
        key=lambda index: abs((history[index].disclosed_at - target).days),
    )
    anchor = history[anchor_index]
    if abs((anchor.disclosed_at - target).days) > _ANCHOR_TOLERANCE_DAYS:
        return None
    if anchor_index + 1 >= len(history):
        return None
    following = history[anchor_index + 1].disclosed_at
    estimated = _shift_year_forward(following)
    if estimated is None or estimated <= asof:
        return None
    return estimated


# 前年同四半期の発表日は決算期変更や休日繰り上げで数週ずれる。これより離れた開示は
# 同一四半期とみなさず、推定を出さない。
_ANCHOR_TOLERANCE_DAYS = 45

# 会社は予定日より数日早く開示することがある。その差を staleness と読むと、数字が
# 現に最新である行に flag が立つ (実測: 予定 7/15 に対し 7/13 開示)。四半期の間隔は
# 90 日前後なので、この幅なら「同じ発表の前倒し」と「前四半期のまま」を取り違えない。
_SAME_EVENT_TOLERANCE_DAYS = 14


def _shift_year(value: date) -> date | None:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 2/29 は前年に存在しない。周期推定にとって 1 日の差は意味がないので寄せる。
        return value.replace(year=value.year - 1, day=28)


def _shift_year_forward(value: date) -> date | None:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def build_earnings_lag(
    *,
    ticker: str,
    asof: date,
    latest_disclosed: Mapping[str, date],
    calendar_next: Mapping[str, date],
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
) -> EarningsLag:
    """1 ticker の annotation を組む。

    ``stale_fin_flag`` は「カレンダーが as-of 以前の発表を指しているのに、store の最新
    開示がそこから四半期分ずれている」で立つ。これは発表が済んだのに数字が追いついて
    いない窓そのもので、判断面で最初に見たい条件である。カレンダーが未来を指す通常状態
    では立たず、予定日より数日早い開示も staleness と読まない。
    """

    disclosed = latest_disclosed.get(ticker)
    announced_on = calendar_next.get(ticker)
    stale = (
        announced_on is not None
        and announced_on <= asof
        and (disclosed is None or (announced_on - disclosed).days > _SAME_EVENT_TOLERANCE_DAYS)
    )
    estimated = (
        None
        if announced_on is not None and announced_on > asof
        else estimate_next_announcement(summaries_by_ticker.get(ticker, ()), asof=asof)
    )
    return EarningsLag(
        fin_latest_disclosed_date=disclosed,
        next_earnings_estimated_date=estimated,
        stale_fin_flag=stale,
    )


def tickers_without_calendar_rows(
    tickers: Sequence[str], calendar_tickers: Mapping[str, date]
) -> int:
    """カレンダー行を 1 つも持たない universe ticker 数。

    provider 側の欠落 = 件数の急増と、個別企業の未公表を区別するための計数で、閾値も判定も
    持たない。
    """

    return sum(1 for ticker in tickers if ticker not in calendar_tickers)


__all__ = [
    "CalendarEntry",
    "EarningsLag",
    "build_earnings_lag",
    "estimate_next_announcement",
    "index_calendar_announcements",
    "latest_disclosed_dates",
    "tickers_without_calendar_rows",
]
