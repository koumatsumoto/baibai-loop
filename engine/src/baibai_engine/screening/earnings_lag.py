"""決算開示と as-of 財務のラグを判断面へ出す annotation。

決算ピーク週では「機械行の数字が直近の発表を含んでいるか」が判断の最初の分岐になる。
その問いに答えるのは 2 つの source の突き合わせである:

- ``jpx_earnings_calendar`` — 会社が公表した次回発表**予定**日。ticker あたり 1 行で、
  会社が予定日より前に開示しても行はそのまま残る。日付だけを見ても「これから」と
  「もう出た」が区別できない。
- ``jquants_fin_summaries`` — 実際に開示された数字。機械行の財務はここから作られる。

この 2 つを突き合わせられる場所は 1 つしかないので、状態の判定はすべてここで行い、
表示層は転記だけをする。ranking にも gate にも E[r] にも入らない。

**historical backfill では読めない**: カレンダーは単一 snapshot で fetch 日を持たないため、
過去 as-of の run は「今日の予定表」を読む。その run の annotation は as-of 時点の状態では
ないので、過去 run の `next_earnings_status` と `stale_fin_flag` を解釈しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .providers.jpx import JPXEarningsCalendarEntry
from .providers.jquants import JQuantsFinancialSummary

type NextEarningsStatus = Literal["announced", "scheduled", "estimated", "unknown"]

# 会社は予定日より数日早く開示することがある。その差を「別の四半期」と読むと、数字が
# 現に最新である行を stale と呼び、逆に発表済みの行を「これから」と呼ぶ。四半期の間隔は
# 90 日前後なので、この幅なら同じ発表の前倒しと前四半期のままを取り違えない。
_SAME_EVENT_TOLERANCE_DAYS = 14

# 前年同期の発表日は決算期変更や休日繰り上げでずれる。これより離れた開示は同一四半期と
# みなさず、推定を出さない。
_ANCHOR_TOLERANCE_DAYS = 45


@dataclass(frozen=True, slots=True, kw_only=True)
class EarningsLag:
    """1 ticker 分の決算ラグ annotation。すべて ranking・gate へ入らない。"""

    fin_latest_disclosed_date: date | None
    next_earnings_estimated_date: date | None
    next_earnings_status: NextEarningsStatus
    # None = 判定材料が無い: 予定日が未来 / カレンダー行が無い / 行に財務が無い。
    # False は「照合して食い違わなかった」であり、両者を同じ値にしない。
    stale_fin_flag: bool | None


def index_calendar_announcements(
    entries: Sequence[JPXEarningsCalendarEntry],
) -> dict[str, date]:
    """ticker ごとの発表予定日。

    network 経路は ticker あたり 1 行を保証するが、cache の primary key は
    ``(announcement_date, ticker)`` なので同じ ticker の 2 行を弾かない。黙って
    どちらかが勝つと「発表済みなのに予定」へ静かに転ぶので、見つけたら止める。
    """

    index: dict[str, date] = {}
    for entry in entries:
        seen = index.get(entry.ticker)
        if seen is not None and seen != entry.announcement_date:
            raise ValueError(
                f"earnings calendar has conflicting rows for {entry.ticker}: "
                f"{seen.isoformat()} and {entry.announcement_date.isoformat()}"
            )
        index[entry.ticker] = entry.announcement_date
    return index


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
        for value in (item.sales, item.cfo, item.profit, item.operating_profit, item.eps_ttm)
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
    fin_latest_disclosed: date | None,
    announcement_date: date | None,
    summaries: Sequence[JQuantsFinancialSummary],
) -> EarningsLag:
    """1 ticker の annotation を組む。

    ``fin_latest_disclosed`` は行の財務が読んだ開示日そのものを受け取る。ここで数え直すと
    「行が使った日」と「annotation が示す日」が静かにずれうるので、導出元は 1 つに保つ。

    状態と flag は同じ 2 入力から同時に決める。別々の層で判定すると、一方が「発表済み」
    と読む行を他方が「これから」と呼ぶ食い違いが起きる。
    """

    estimated = (
        None
        if announcement_date is not None and announcement_date > asof
        else estimate_next_announcement(summaries, asof=asof)
    )
    return EarningsLag(
        fin_latest_disclosed_date=fin_latest_disclosed,
        next_earnings_estimated_date=estimated,
        next_earnings_status=_status(
            asof=asof, announcement_date=announcement_date, estimated=estimated
        ),
        stale_fin_flag=_stale_fin_flag(
            asof=asof,
            announcement_date=announcement_date,
            fin_latest_disclosed=fin_latest_disclosed,
        ),
    )


def _status(
    *, asof: date, announcement_date: date | None, estimated: date | None
) -> NextEarningsStatus:
    """予定日と as-of の前後だけで決める。

    「予定日の手前に開示があるから前倒しで発表済み」という読みは試したが、実 store の
    retrospective で 89% が誤りだった —— 決算直前の業績予想修正・再開示が同じ形に見え、
    本番の開示は予定どおり来る。誤って ``announced`` にすると読み手は目前の決算を
    event risk から外すので、判定できないほうへ倒す。前倒し開示した銘柄はカレンダーが
    更新されるまで ``scheduled`` に見えるが、隣の ``fin_latest_disclosed_date`` が
    予定日の直前を指すので読み手はそこで気づける。
    """

    if announcement_date is not None:
        return "announced" if announcement_date <= asof else "scheduled"
    return "estimated" if estimated is not None else "unknown"


def _stale_fin_flag(
    *, asof: date, announcement_date: date | None, fin_latest_disclosed: date | None
) -> bool | None:
    """予定日に対応する開示が行に無いか。原因は区別しない。

    立つのは「予定日が as-of 以前なのに、その発表に対応する statement が行に無い」場合で、
    延期・決算期変更・provider 欠落のいずれでも立つ。どれであっても読み手のすべきことは
    同じ = 一次開示で切り分ける、なので、原因を断定しない。判定材料が無ければ ``None``。
    """

    if announcement_date is None or announcement_date > asof or fin_latest_disclosed is None:
        return None
    return (announcement_date - fin_latest_disclosed).days > _SAME_EVENT_TOLERANCE_DAYS


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
    "index_calendar_announcements",
    "tickers_without_calendar_rows",
]
