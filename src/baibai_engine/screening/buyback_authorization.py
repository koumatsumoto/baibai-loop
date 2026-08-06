"""自己株式取得枠の提出状況を判断面へ出す annotation。

E[r] の carry は `dividend_yield + clip(-net_share_change_yoy, ±5%)` で、buyback 側は
**過去 1 年の株数変化**である。取得枠を消化し終えた会社もこの成分を持ち続けるので、
carry を「これから受け取る現金還元」と読むと過大評価になる。

読み分けの手掛かりは EDINET にある。自己株券買付状況報告書 — doc_type 220、訂正は
230 — は金商法 24 条の 6 第 1 項により、取締役会決議による取得の**取得期間中は毎月**
提出される。したがって提出の有無と齢が、取得枠がいつまで在ったかの観測になる。

**観測できるのは「直近の報告月に取得枠が在った」までで、「今も在る」ではない。** 提出は
報告月の翌月に出るので、取得期間が終了した月の報告書も期間終了後に提出される。実例:
6088 は 2026-08-05 に提出 — 齢 0 日 — だが、その中身は「取得期間 2026-05-11〜2026-07-31、
金額進捗 99.99%」で、同日に取得終了が開示されている。残枠と取得期間の終了日は本 module
では読まないので、carry を forward の現金還元として扱うなら一次開示で確認する。

**この annotation は E[r]・ranking・gate を変えない。** 較正リプレイでは、株数減少群は
取得が単発で終わった銘柄も含めて母集団を上回っており、carry を落とす変更は実在する
予測情報を削る。計測は `reports/2026-08-06-bargain-capture-diagnosis.md` にある。ここで
出すのは、その carry が forward の現金還元なのか資本配分の質のマーカーなのかを
research が判断するための素材である。

観測窓の限界を status で表す。store が as-of から遡って必要な窓を持たないときに
「提出なし」と「未観測」を同じ値へ畳むと、historical backfill が枠の不在を捏造する。

SQLite の読み取りをこの module に置くのは、`sqlite_reader` が EDINET metric extractor の
import closure に入っているためである。そちらへ足すと、この annotation を編集するたびに
extractor revision が動き、再利用できる EDINET metric baseline が捨てられる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from baibai_engine.market.sqlite import connect_current

# 値は観測そのものを表す。枠が今も在るかの推論は読み手が一次開示で決める。
type BuybackAuthorizationStatus = Literal["recent_filing", "stale_filing", "no_filing", "unknown"]

# 自己株券買付状況報告書は報告月の翌月 15 日までに提出される。取得期間中の会社は毎月
# 提出するので、期間中であれば直近の提出はこの日数以内に収まる。月初の as-of で前月分が
# 未提出でも、前々月分が窓に入る幅を取っている。取得期間が終了した月の報告書もこの窓に
# 入るので、`recent_filing` は「終了直後」を含む。
RECENT_FILING_WINDOW_DAYS = 45

# 「提出が 1 度も無い」と言うために必要な観測窓。1 年あれば、年 1 回だけ枠を設ける
# 会社も窓の中に現れる。
OBSERVATION_WINDOW_DAYS = 365

# EDINET の様式コード。自己株券買付状況報告書とその訂正。
BUYBACK_STATUS_FILING_DOC_TYPE = "220"
BUYBACK_STATUS_CORRECTION_DOC_TYPE = "230"


@dataclass(frozen=True, slots=True, kw_only=True)
class BuybackStatusFilingRead:
    """自己株券買付状況報告書の観測。観測窓を伴わない最新提出日は解釈できない。"""

    latest_filing_by_ticker: Mapping[str, date]
    observed_from: date | None


@dataclass(frozen=True, slots=True, kw_only=True)
class BuybackAuthorization:
    """1 ticker 分の取得枠 annotation。ranking・gate・E[r] へは入らない。"""

    status: BuybackAuthorizationStatus
    # 直近の自己株券買付状況報告書の提出日。status が unknown / no_filing なら None。
    latest_filing_date: date | None
    # as-of から見た提出の齢。読み手が自分の閾値で判断できるように生値を出す。
    latest_filing_age_days: int | None
    # 観測できた窓。status を解釈するときの前提そのものなので必ず併記する。
    observed_from: date | None


def index_buyback_status_filings(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, date]:
    """ticker ごとの直近提出日。

    `sec_code` は 5 桁 `60880` の形なので先頭 4 桁を ticker とする。証券コードを持たない
    提出者、たとえば非上場の子会社は候補と突き合わせられないので落とす。
    """

    latest: dict[str, date] = {}
    for row in rows:
        ticker = _ticker(row.get("sec_code"))
        if ticker is None:
            continue
        filed = _date(row.get("doc_date"))
        if filed is None:
            continue
        seen = latest.get(ticker)
        if seen is None or filed > seen:
            latest[ticker] = filed
    return latest


def read_buyback_status_filings(
    sqlite_path: Path, *, through: date
) -> BuybackStatusFilingRead | None:
    """ticker 別の最新提出日と、store が実際に観測できている最古の提出日。

    観測窓を提出行そのものから取るのは、EDINET 文書一覧を fetch していても当該 doc type を
    保存していなかった期間があるためで、一覧の取得記録を窓とみなすと「提出なし」を捏造する。
    """

    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    doc_types = (BUYBACK_STATUS_FILING_DOC_TYPE, BUYBACK_STATUS_CORRECTION_DOC_TYPE)
    through_iso = through.isoformat()
    try:
        rows = conn.execute(
            "SELECT sec_code, MAX(doc_date) FROM edinet_documents "
            "WHERE doc_type_code IN (?, ?) AND sec_code IS NOT NULL AND doc_date <= ? "
            "GROUP BY substr(sec_code, 1, 4)",
            (*doc_types, through_iso),
        ).fetchall()
        observed_months = [
            str(month)
            for (month,) in conn.execute(
                "SELECT DISTINCT substr(doc_date, 1, 7) FROM edinet_documents "
                "WHERE doc_type_code IN (?, ?) AND doc_date <= ? "
                "ORDER BY 1",
                (*doc_types, through_iso),
            )
        ]
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return BuybackStatusFilingRead(
        latest_filing_by_ticker=index_buyback_status_filings(
            [{"sec_code": sec_code, "doc_date": filed} for sec_code, filed in rows]
        ),
        observed_from=_continuous_observation_start(observed_months, through=through),
    )


def _continuous_observation_start(observed_months: Sequence[str], *, through: date) -> date | None:
    """as-of から遡って、提出が途切れずに観測できている最古の月初。

    最古の 1 行だけを窓の左端にすると、2 通りの形で「提出なし」を捏造する。1 社の古い提出が
    1 行あるだけで universe 全体が観測済みになり、EDINET 取得が数週間止まっても左端は古い
    ままなので `unknown` へ落ちない。取得期間中の会社は毎月提出するので、月次の連続性が
    そのまま観測の連続性になる。
    """

    months = set(observed_months)
    if not months:
        return None
    cursor = through.replace(day=1)
    if cursor.strftime("%Y-%m") not in months:
        # 当月分はまだ 1 件も出ていないことがある。直前月まで下がって連続性を見る。
        cursor = _previous_month(cursor)
    start: date | None = None
    while cursor.strftime("%Y-%m") in months:
        start = cursor
        cursor = _previous_month(cursor)
    return start


def _previous_month(first_of_month: date) -> date:
    if first_of_month.month == 1:
        return first_of_month.replace(year=first_of_month.year - 1, month=12)
    return first_of_month.replace(month=first_of_month.month - 1)


def build_buyback_authorization(
    *,
    asof: date,
    latest_filing_date: date | None,
    observed_from: date | None,
) -> BuybackAuthorization:
    """観測窓を踏まえて取得枠の状態を決める。

    `observed_from` は as-of から遡って提出が途切れずに観測できている最古の月初である。
    `edinet_document_lists` の取得記録ではなく提出行そのものから取るのは、doc 一覧を
    fetch していても当該 doc type を保存していなかった期間があるためで、そこを覆えていると
    数えると「提出なし」を捏造する。連続性まで見るのは、途中に取得の穴があると、その月に
    提出した会社が一斉に「提出なし」へ落ちるためである。
    """

    required_from = asof - timedelta(days=OBSERVATION_WINDOW_DAYS)
    if observed_from is None or observed_from > required_from:
        return BuybackAuthorization(
            status="unknown",
            latest_filing_date=None,
            latest_filing_age_days=None,
            observed_from=observed_from,
        )
    if latest_filing_date is None or latest_filing_date > asof:
        return BuybackAuthorization(
            status="no_filing",
            latest_filing_date=None,
            latest_filing_age_days=None,
            observed_from=observed_from,
        )
    age_days = (asof - latest_filing_date).days
    return BuybackAuthorization(
        status=("recent_filing" if age_days <= RECENT_FILING_WINDOW_DAYS else "stale_filing"),
        latest_filing_date=latest_filing_date,
        latest_filing_age_days=age_days,
        observed_from=observed_from,
    )


def _ticker(value: object) -> str | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    ticker = value[:4]
    return ticker if ticker.isalnum() else None


def _date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "BUYBACK_STATUS_CORRECTION_DOC_TYPE",
    "BUYBACK_STATUS_FILING_DOC_TYPE",
    "OBSERVATION_WINDOW_DAYS",
    "RECENT_FILING_WINDOW_DAYS",
    "BuybackAuthorization",
    "BuybackAuthorizationStatus",
    "BuybackStatusFilingRead",
    "build_buyback_authorization",
    "index_buyback_status_filings",
    "read_buyback_status_filings",
]
