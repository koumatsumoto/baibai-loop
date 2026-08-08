"""自己株式取得枠の状態を判断面へ出す annotation。

E[r] の carry は `dividend_yield + clip(-net_share_change_yoy, ±5%)` で、buyback 側は
**過去 1 年の株数変化**である。取得枠を消化し終えた会社もこの成分を持ち続けるので、
carry を「これから受け取る現金還元」と読むと過大評価になる。

読み分けの手掛かりは EDINET にある。自己株券買付状況報告書 — doc_type 220、訂正は
230 — は金商法 24 条の 6 第 1 項により、取締役会決議による取得の**取得期間中は毎月**
提出される。したがって提出の有無と齢が、取得枠がいつまで在ったかの観測になる。

提出の有無だけでは「直近の報告月に取得枠が在った」までしか言えない。提出は報告月の翌月に
出るので、取得期間が終了した月の報告書も期間終了後に提出される。6088 は 2026-08-05 提出で
齢 0 日だが、中身は取得期間 2026-05-11〜2026-07-31・金額進捗 99.99% で、同日に取得
終了が開示されている。そこを埋めるのが `remaining_share_ratio` と
`authorization_window_end` で、様式そのものが月次で出している値から読む。

読めなかった値は欠損のまま置く。残枠が読めないのに 0 を返すと「枠を使い切った」と主張する
ことになり、carry の読み方を逆に倒す。

**この annotation は E[r]・ranking・gate を変えない。** 較正リプレイでは、株数減少群は
取得が単発で終わった銘柄も含めて母集団を上回っており、carry を落とす変更は実在する
予測情報を削る。計測は
`reports/studies/2026-08-06-bargain-capture-diagnosis/report.md` にある。ここで
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
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from baibai_engine.market.sqlite import connect_current

from .buyback_store import StoredBuybackReport

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
    # 直近報告月末時点で、決議した株式数のうちまだ買っていない割合。1.0 なら手つかず、
    # 0.0 なら使い切り。`None` は「読めなかった」であって「残っていない」ではない。
    remaining_share_ratio: float | None = None
    # 直近 3 報告月に取得した株数 ÷ 発行済株式総数。carry の buyback 成分は trailing の
    # 株数変化なので、取得を始めたばかりの会社はそこにまだ現れない。こちらは現れる。
    trailing_3m_acquired_ratio: float | None = None
    # 取得期間の終了日。過ぎていれば、直近の提出があっても枠はもう無い。
    authorization_window_end: date | None = None
    # 上の 3 つが由来する報告月末。提出日とは 2 週間から 1 か月ずれる。
    report_month_end: date | None = None


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
    """ticker 別の最新提出日と、検証済み日次一覧が連続する最古の日。

    特定様式の提出行は positive evidence にしかならない。提出がないことを主張するには、
    EDINET の全様式を含む日次一覧について final、metadata count、永続行数、source coverage
    が一致した日だけを universe-wide coverage として数える。
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
        observed_from = _validated_document_observation_start(conn, through=through)
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return BuybackStatusFilingRead(
        latest_filing_by_ticker=index_buyback_status_filings(
            [{"sec_code": sec_code, "doc_date": filed} for sec_code, filed in rows]
        ),
        observed_from=observed_from,
    )


def _validated_document_observation_start(
    conn: sqlite3.Connection, *, through: date
) -> date | None:
    """Return the start of the contiguous, fully validated daily document-list suffix."""

    rows = conn.execute(
        "SELECT l.doc_date, l.result_count, l.is_final, "
        "c.coverage_start, c.coverage_end, c.record_count, c.status, c.error, "
        "(SELECT COUNT(*) FROM edinet_documents d WHERE d.doc_date = l.doc_date) "
        "FROM edinet_document_lists l LEFT JOIN source_coverage c "
        "ON c.source = 'edinet_documents' AND c.coverage_key = l.doc_date "
        "WHERE l.doc_date <= ? ORDER BY l.doc_date DESC",
        (through.isoformat(),),
    ).fetchall()
    by_day = {str(row[0]): row for row in rows}
    cursor = through
    start: date | None = None
    while (row := by_day.get(cursor.isoformat())) is not None:
        expected = int(row[1])
        valid = (
            int(row[2]) == 1
            and row[3] == cursor.isoformat()
            and row[4] == cursor.isoformat()
            and row[5] is not None
            and int(row[5]) == expected
            and row[6] == "ok"
            and row[7] is None
            and int(row[8]) == expected
        )
        if not valid:
            break
        start = cursor
        cursor -= timedelta(days=1)
    return start


def build_buyback_authorization(
    *,
    asof: date,
    latest_filing_date: date | None,
    observed_from: date | None,
) -> BuybackAuthorization:
    """観測窓を踏まえて取得枠の状態を決める。

    `observed_from` は as-of から遡って、final な日次一覧と source coverage と永続行数が
    一致し続ける最古の日である。途中の欠落・partial・件数不一致・未確定日はそこで窓を切り、
    特定様式の提出行があるだけでは universe 全体を観測済みとみなさない。
    """

    # A filing observed by the as-of is positive evidence even when the store has not
    # accumulated a full no-filing window.  The year-long window is required only for
    # the negative claim that no authorization filing exists.
    if latest_filing_date is not None and latest_filing_date <= asof:
        age_days = (asof - latest_filing_date).days
        return BuybackAuthorization(
            status=("recent_filing" if age_days <= RECENT_FILING_WINDOW_DAYS else "stale_filing"),
            latest_filing_date=latest_filing_date,
            latest_filing_age_days=age_days,
            observed_from=observed_from,
        )
    required_from = asof - timedelta(days=OBSERVATION_WINDOW_DAYS)
    if observed_from is None or observed_from > required_from:
        return BuybackAuthorization(
            status="unknown",
            latest_filing_date=None,
            latest_filing_age_days=None,
            observed_from=observed_from,
        )
    return BuybackAuthorization(
        status="no_filing",
        latest_filing_date=None,
        latest_filing_age_days=None,
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
    "ACQUISITION_PACE_MONTHS",
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


# 取得ペースを測る窓。様式は月次なので 3 報告月 = 概ね四半期で、carry の trailing 1 年より
# ずっと手前の動きを拾う。
ACQUISITION_PACE_MONTHS = 3


def with_authorization_state(
    annotation: BuybackAuthorization,
    reports: Sequence[StoredBuybackReport],
) -> BuybackAuthorization:
    """提出の有無だけの annotation へ、様式が出している枠の状態を足す。

    直近報告月の残枠と取得期間の終了日、そして直近 3 報告月の取得ペースを載せる。status
    そのものは変えない — 観測窓の話と枠の中身の話は別で、畳むと「読めなかった」が
    「枠が無い」に化ける。
    """

    if not reports:
        return annotation
    latest = reports[0]
    remaining_ratio: float | None = None
    if (
        latest.resolved_shares is not None
        and latest.cumulative_shares is not None
        and latest.resolved_shares > 0
    ):
        remaining = max(latest.resolved_shares - latest.cumulative_shares, 0)
        remaining_ratio = remaining / latest.resolved_shares
    acquired = [
        report.month_shares
        for report in reports[:ACQUISITION_PACE_MONTHS]
        if report.month_shares is not None
    ]
    pace: float | None = None
    issued = latest.issued_shares
    # 1 か月でも読めない月があれば合計は過少になるので、揃っているときだけ答える。
    if issued and issued > 0 and len(acquired) == min(ACQUISITION_PACE_MONTHS, len(reports)):
        pace = sum(acquired) / issued
    return replace(
        annotation,
        remaining_share_ratio=remaining_ratio,
        trailing_3m_acquired_ratio=pace,
        authorization_window_end=latest.window_end,
        report_month_end=latest.report_month_end,
    )
