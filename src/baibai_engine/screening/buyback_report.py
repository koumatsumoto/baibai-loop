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

_BOARD_BLOCK = "AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock"
_HOLDING_BLOCK = "HoldingOfTreasurySharesTextBlock"
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

    report = BuybackReport(**fields)  # type: ignore[arg-type]
    return _drop_inconsistent(report)


def _drop_inconsistent(report: BuybackReport) -> BuybackReport:
    """Discard a reading that contradicts the form rather than passing it downstream.

    A label-anchored read can in principle latch onto the wrong number if a filing
    reshapes its table. These two relations always hold in the real form, so a violation
    means the read is wrong — and a wrong buyback size is worse than an absent one.
    """
    updates: dict[str, None] = {}
    if (
        report.resolved_shares is not None
        and report.cumulative_shares is not None
        and report.cumulative_shares > report.resolved_shares
    ):
        updates["resolved_shares"] = None
        updates["resolved_amount_yen"] = None
        updates["cumulative_shares"] = None
        updates["cumulative_amount_yen"] = None
    if (
        report.issued_shares is not None
        and report.treasury_shares is not None
        and report.treasury_shares > report.issued_shares
    ):
        updates["issued_shares"] = None
        updates["treasury_shares"] = None
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
            )
        }
    )
