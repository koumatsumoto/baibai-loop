"""Read the state of a share buyback authorization out of a form-220 filing.

自己株券買付状況報告書 states the authorized size, what has been bought against it, and
the window it runs in. Those are the forward-looking part of the buyback: the carry term
in E[r] is a *trailing* share-count change, so a company that has just started buying
does not show up in it yet, and one that finished months ago still does.

The numbers live inside XBRL TextBlock elements as a flattened HTML table, not as typed
facts, so this reads them by label. Two properties make that safe rather than a guess:

- comma-grouped integers stay unambiguous when concatenated, because a group is exactly
  three digits after a comma — a greedy match over `600,000300,000,000` stops after
  `600,000` and the next match starts at `300,000,000`
- the percentages the form also prints are *not* read. `88.9299.99` is two numbers with
  no separator and no fixed decimal width, so the progress is derived from the two
  integer counts instead

Anything that does not match is left absent. A buyback whose size cannot be read must not
arrive as a zero: that would claim the authorization is exhausted.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Literal

_BOARD_BLOCK = "AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock"
_HOLDING_BLOCK = "HoldingOfTreasurySharesTextBlock"
_DISPOSALS_BLOCK = "DisposalsOfTreasurySharesTextBlock"
_REPORTING_PERIOD = "ReportingPeriodCoverPage"

# A comma-grouped integer. The three-digit groups are what make a run of concatenated
# numbers separable. The leading digit may not be zero unless the number *is* zero:
# without that, `0300,000,000` splits as `030` and loses the boundary, because a greedy
# three-digit head would swallow the start of the next number.
_INT = r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})*)"
# Filings mix full-width and half-width brackets, and the dash in the total row varies
# across three code points, so the labels are matched permissively.
_DATE = r"[0-9]{4}年[０-９0-9]{1,2}月[０-９0-9]{1,2}日"
_SPACE = r"[\s　]*"

_RESOLUTION = re.compile(
    rf"取得期間{_SPACE}(?P<start>{_DATE}){_SPACE}[〜～~]{_SPACE}(?P<end>{_DATE})"
    rf"[)）]?{_SPACE}(?P<shares>{_INT})(?P<amount>{_INT})"
)
_CUMULATIVE = re.compile(
    rf"報告月末現在の累[計積]取得自己株式{_SPACE}(?P<shares>{_INT})(?P<amount>{_INT})"
)
_MONTHLY = re.compile(rf"計[－―─\-]{_SPACE}(?P<shares>{_INT})(?P<amount>{_INT})")
_HOLDING = re.compile(
    rf"発行済株式総数{_SPACE}(?P<issued>{_INT})保有自己株式数{_SPACE}(?P<treasury>{_INT})"
)
_PERIOD_END = re.compile(rf"至{_SPACE}(?P<end>{_DATE})")
_DISPOSAL_ROW = re.compile(rf"[０-９0-9]{{1,2}}月[０-９0-9]{{1,2}}日{_SPACE}(?P<shares>{_INT})")
_ZERO_DISPOSAL_ROW = re.compile(r"[-－―─]+月[-－―─]+日")
_NO_DISPOSITION = re.compile(r"【処理状況】\s*該当事項はありません")
_DISPOSAL_CATEGORY = re.compile(
    r"(?P<recruitment>引き受ける者の募集を行った取得自己株式)"
    r"|(?P<cancellation>消却の処分を行った取得自己株式)"
    r"|(?P<reorganization>合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式)"
    r"|(?P<other>その他(?:（|\()[^）)]*(?:）|\)))"
    r"|(?P<total>合計)"
)

type BuybackDispositionPurpose = Literal[
    "cancellation",
    "employee_compensation_esop",
    "other_rerelease",
]


class BuybackReportError(ValueError):
    """Raised when the filing cannot be opened at all."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BuybackReport:
    """One monthly report against one board authorization.

    Every field is optional because the form omits whole sections when they do not
    apply: a company with no board authorization files the holding section alone.
    """

    report_month_end: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    resolved_shares: int | None = None
    resolved_amount_yen: int | None = None
    cumulative_shares: int | None = None
    cumulative_amount_yen: int | None = None
    month_shares: int | None = None
    month_amount_yen: int | None = None
    issued_shares: int | None = None
    treasury_shares: int | None = None
    # Actual treasury-share actions in this monthly filing.  This is not the board's
    # future intent; it records how shares had been used by the filing date.
    disposition_purposes: tuple[BuybackDispositionPurpose, ...] = ()
    disposition_observed: bool = False

    @property
    def remaining_shares(self) -> int | None:
        """Shares still authorized but not yet bought.

        `None` when either side is unreadable — the difference between "the authorization
        is spent" and "we cannot tell" is the whole point of this record.
        """
        if self.resolved_shares is None or self.cumulative_shares is None:
            return None
        return max(self.resolved_shares - self.cumulative_shares, 0)

    @property
    def consumed_ratio(self) -> float | None:
        """How much of the authorized share count has been bought, 0.0 to 1.0."""
        if self.resolved_shares is None or self.cumulative_shares is None:
            return None
        if self.resolved_shares <= 0:
            return None
        return min(self.cumulative_shares / self.resolved_shares, 1.0)


def _to_int(value: str) -> int:
    return int(value.replace(",", ""))


def _to_date(value: str) -> date | None:
    digits = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", digits)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _text_blocks(content: bytes) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise BuybackReportError(f"buyback filing is not a readable archive: {error}") from error
    blocks: dict[str, str] = {}
    with archive:
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            try:
                decoded = archive.read(name).decode("utf-16")
            except (UnicodeDecodeError, OSError) as error:
                raise BuybackReportError(f"buyback filing CSV is unreadable: {error}") from error
            for row in csv.reader(io.StringIO(decoded), delimiter="\t"):
                if len(row) >= 2:
                    blocks[row[0]] = row[-1]
    return blocks


def _named(blocks: dict[str, str], suffix: str) -> str:
    for element, value in blocks.items():
        if element.endswith(suffix):
            return value
    return ""


def parse_buyback_report(content: bytes) -> BuybackReport:
    """Read one form-220 CSV archive into the authorization state it reports."""
    blocks = _text_blocks(content)
    board = _named(blocks, _BOARD_BLOCK)
    holding = _named(blocks, _HOLDING_BLOCK)
    disposals = _named(blocks, _DISPOSALS_BLOCK)
    period = _named(blocks, _REPORTING_PERIOD)

    fields: dict[str, object] = {}
    if match := _PERIOD_END.search(period):
        fields["report_month_end"] = _to_date(match.group("end"))
    if match := _RESOLUTION.search(board):
        fields["window_start"] = _to_date(match.group("start"))
        fields["window_end"] = _to_date(match.group("end"))
        fields["resolved_shares"] = _to_int(match.group("shares"))
        fields["resolved_amount_yen"] = _to_int(match.group("amount"))
    if match := _CUMULATIVE.search(board):
        fields["cumulative_shares"] = _to_int(match.group("shares"))
        fields["cumulative_amount_yen"] = _to_int(match.group("amount"))
    if match := _MONTHLY.search(board):
        fields["month_shares"] = _to_int(match.group("shares"))
        fields["month_amount_yen"] = _to_int(match.group("amount"))
    if match := _HOLDING.search(holding):
        fields["issued_shares"] = _to_int(match.group("issued"))
        fields["treasury_shares"] = _to_int(match.group("treasury"))
    disposition = classify_buyback_disposition(disposals)
    if disposition is not None:
        fields["disposition_observed"] = True
        fields["disposition_purposes"] = disposition

    report = BuybackReport(**fields)  # type: ignore[arg-type]
    return _drop_inconsistent(report)


def classify_buyback_disposition(
    text: str,
) -> tuple[BuybackDispositionPurpose, ...] | None:
    """Classify actual cancellation/re-release rows in a form-220 disposal block.

    ``None`` means the block was unavailable.  An empty tuple means it was observed and
    reported no positive action.  Category labels alone are not evidence because the
    statutory table prints every category with dashes when nothing happened.
    """

    if not text:
        return None
    if _NO_DISPOSITION.search(text):
        return ()
    categories = list(_DISPOSAL_CATEGORY.finditer(text))
    expected_categories = ("recruitment", "cancellation", "reorganization", "other", "total")
    if tuple(match.lastgroup for match in categories) != expected_categories:
        return None
    purposes: set[BuybackDispositionPurpose] = set()
    for index, match in enumerate(categories):
        category = match.lastgroup
        if category == "total":
            continue
        end = categories[index + 1].start() if index + 1 < len(categories) else len(text)
        segment = text[match.end() : end]
        positive_rows = [
            row for row in _DISPOSAL_ROW.finditer(segment) if _to_int(row.group("shares")) > 0
        ]
        if not positive_rows and _ZERO_DISPOSAL_ROW.search(segment) is None:
            # A known heading with an unrecognized/truncated row is unknown, not an
            # observed zero.  Form layout changes must not silently become evidence.
            return None
        if not positive_rows:
            continue
        if category == "cancellation":
            purposes.add("cancellation")
        elif category == "other" and any(
            keyword in match.group(0)
            for keyword in ("従業員", "持株会", "株式報酬", "ストックオプション")
        ):
            purposes.add("employee_compensation_esop")
        else:
            purposes.add("other_rerelease")
    order: tuple[BuybackDispositionPurpose, ...] = (
        "cancellation",
        "employee_compensation_esop",
        "other_rerelease",
    )
    return tuple(purpose for purpose in order if purpose in purposes)


# 1 株当たり取得価額の上限。store 全 5,277 行の実測は 68,185 円までが連続し、その上は
# 711,141 円と 4,800,600 円の 2 行しか無い。どちらも累計株数が 700 株・1 株という桁落ちの
# 形で、金額の側は正しい。境界はこの空白の中央に置く。
#
# 境界を実データの縁ではなく中央に置くのは、上側が普通株の価格帯と近いからである。終値
# 10 万円超は 58 銘柄あり 57 銘柄は ETF と REIT だが、285A は分割を経ずに 108,700 円まで
# 上げた事業会社で、6146 / 9983 / 6861 も 87,000 円台にある。買付を出す銘柄の単価最大は
# 68,185 円 (6273) で年 31% 上がっている。上限を実勢の縁へ置くと、正しい行が黙って落ちる。
#
# 決議単価 (`resolved_amount_yen / resolved_shares`) との比で判定する案は採らない。決議は
# 株数と金額を独立に決めるので比は市場価格を表さず、実測では正しい行 (6078: 単価 1,406 円・
# 当日終値 1,416 円) を決議単価 143 円との比 9.8 倍で落とす。
#
# それでも固定値である以上いつかは実勢に追われる。落ちた件数は run の fallback 行へ出るので、
# 前提が崩れるときはそちらが先に動く。
_MAX_PLAUSIBLE_UNIT_PRICE_YEN = 300_000


def inconsistent_buyback_fields(
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    resolved_shares: int | None = None,
    resolved_amount_yen: int | None = None,
    cumulative_shares: int | None = None,
    cumulative_amount_yen: int | None = None,
    month_shares: int | None = None,
    issued_shares: int | None = None,
    treasury_shares: int | None = None,
) -> frozenset[str]:
    """The fields whose values contradict the form and must not reach a judgement.

    A label-anchored read can latch onto the wrong number if a filing reshapes its table,
    and the concatenated-integer split can end early and take only the leading digits of
    a number. These relations always hold in the real form, so a violation means the read
    is wrong — and a wrong buyback size is worse than an absent one.

    **桁落ちは値を小さくするので、上限だけを見る検査では捕まらない。** 決議株数を超える
    累計という形は store 全 4,904 行で 1 件も無い一方、累計が前月より減る行は 13 件ある。
    下限側の関係 (月次取得は累計の一部である・1 株当たり価額は実在する水準である) を
    併せて見る。

    取り込み時と読み取り時の両方から呼ぶ。規則が書かれる前に保存された行は取り込みを
    やり直さないと直らないが、読み取り側で同じ規則を通せば判断面へは出ない。
    """
    dropped: set[str] = set()
    if (
        resolved_shares is not None
        and cumulative_shares is not None
        and cumulative_shares > resolved_shares
    ):
        dropped.update(
            {
                "resolved_shares",
                "resolved_amount_yen",
                "cumulative_shares",
                "cumulative_amount_yen",
            }
        )
    if (
        resolved_amount_yen is not None
        and cumulative_amount_yen is not None
        and cumulative_amount_yen > resolved_amount_yen
    ):
        dropped.update(
            {
                "resolved_shares",
                "resolved_amount_yen",
                "cumulative_shares",
                "cumulative_amount_yen",
            }
        )
    # 報告月の取得は累計に含まれるので、累計を上回ることはない。桁落ちした累計はこの
    # 関係を破る側へ倒れる。どちらが壊れたかは行からは決められないので両方落とす。
    if (
        month_shares is not None
        and cumulative_shares is not None
        and month_shares > cumulative_shares
    ):
        dropped.update(
            {"cumulative_shares", "cumulative_amount_yen", "month_shares", "month_amount_yen"}
        )
    if (
        cumulative_shares is not None
        and cumulative_shares > 0
        and cumulative_amount_yen is not None
        and cumulative_amount_yen / cumulative_shares > _MAX_PLAUSIBLE_UNIT_PRICE_YEN
    ):
        dropped.update({"cumulative_shares", "cumulative_amount_yen"})
    if window_start is not None and window_end is not None and window_start > window_end:
        dropped.update({"window_start", "window_end"})
    if (
        issued_shares is not None
        and treasury_shares is not None
        and treasury_shares > issued_shares
    ):
        dropped.update({"issued_shares", "treasury_shares"})
    return frozenset(dropped)


def _drop_inconsistent(report: BuybackReport) -> BuybackReport:
    """Apply `inconsistent_buyback_fields` to a freshly parsed filing."""
    updates: dict[str, None] = dict.fromkeys(
        inconsistent_buyback_fields(
            window_start=report.window_start,
            window_end=report.window_end,
            resolved_shares=report.resolved_shares,
            resolved_amount_yen=report.resolved_amount_yen,
            cumulative_shares=report.cumulative_shares,
            cumulative_amount_yen=report.cumulative_amount_yen,
            month_shares=report.month_shares,
            issued_shares=report.issued_shares,
            treasury_shares=report.treasury_shares,
        )
    )
    if not updates:
        return report
    return BuybackReport(
        **{
            field: updates.get(field, getattr(report, field))
            for field in (
                "report_month_end",
                "window_start",
                "window_end",
                "resolved_shares",
                "resolved_amount_yen",
                "cumulative_shares",
                "cumulative_amount_yen",
                "month_shares",
                "month_amount_yen",
                "issued_shares",
                "treasury_shares",
                "disposition_purposes",
                "disposition_observed",
            )
        }
    )
