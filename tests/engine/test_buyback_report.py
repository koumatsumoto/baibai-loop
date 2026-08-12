from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import pytest

from baibai_engine.screening.buyback_report import (
    BuybackReportError,
    parse_buyback_report,
)

_PREFIX = "jpcrp-sbr_cor:"

# Shaped after real 2026 filings: the table is flattened, the numbers run together, and
# the bracket and dash characters differ between filers.
_BOARD_FULL_WIDTH = (
    "（２）【取締役会決議による取得の状況】2026年７月31日現在 区分株式数（株）価額の総額（円）"
    "取締役会（2026年５月８日）での決議状況（取得期間　2026年５月11日～2026年７月31日）"
    "600,000300,000,000報告月における取得自己株式（取得日）７月１日10,7005,756,000 "
    "７月31日9,7005,847,500計－220,400127,366,900"
    "報告月末現在の累計取得自己株式533,500299,960,100"
    "自己株式取得の進捗状況（％）88.9299.99"
)
_BOARD_HALF_WIDTH = (
    "2026年７月31日現在区分株式数(株)価額の総額(円)"
    "取締役会(2026年５月14日)での決議状況(取得期間2026年５月15日～2026年７月30日) "
    "6,000,00010,000,000,000報告月における取得自己株式(取得日)７月１日122,500233,607,550 "
    "計―2,156,2003,955,550,250報告月末現在の累積取得自己株式 5,583,7009,999,824,900"
    "自己株式取得の進捗状況(％) 93.06100.00"
)
_HOLDING = (
    "３【保有状況】2026年７月31日現在 報告月末日における保有状況株式数（株）"
    "発行済株式総数86,000,000保有自己株式数4,504,427"
)
_PERIOD = "自　2026年７月１日　至　2026年７月31日"


def _filing(**blocks: str) -> bytes:
    rows = [[f"{_PREFIX}{name}", "", value] for name, value in blocks.items()]
    text = "\n".join("\t".join(row) for row in rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/jpcrp170000-sbr-001.csv", text.encode("utf-16"))
    return buffer.getvalue()


def _default_filing(board: str = _BOARD_FULL_WIDTH) -> bytes:
    return _filing(
        ReportingPeriodCoverPage=_PERIOD,
        AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock=board,
        HoldingOfTreasurySharesTextBlock=_HOLDING,
    )


def test_a_full_width_filing_yields_the_authorization_state() -> None:
    report = parse_buyback_report(_default_filing())

    assert report.report_month_end == date(2026, 7, 31)
    assert report.window_start == date(2026, 5, 11)
    assert report.window_end == date(2026, 7, 31)
    assert report.resolved_shares == 600_000
    assert report.resolved_amount_yen == 300_000_000
    assert report.cumulative_shares == 533_500
    assert report.month_shares == 220_400
    assert report.issued_shares == 86_000_000
    assert report.treasury_shares == 4_504_427
    # 600,000 - 533,500. The filing's own 88.92% is never parsed: `88.9299.99` has no
    # separator and no fixed decimal width, so the ratio is derived from the counts.
    assert report.remaining_shares == 66_500
    assert report.consumed_ratio == pytest.approx(0.889166, rel=1e-4)


def test_half_width_brackets_and_the_other_dash_and_wording_still_parse() -> None:
    """One filer writes 累積 where another writes 累計, and the dash differs too."""

    report = parse_buyback_report(_default_filing(_BOARD_HALF_WIDTH))

    assert report.resolved_shares == 6_000_000
    assert report.cumulative_shares == 5_583_700
    assert report.month_shares == 2_156_200
    assert report.window_end == date(2026, 7, 30)
    assert report.remaining_shares == 416_300


def test_concatenated_integers_split_on_the_comma_grouping() -> None:
    """`600,000300,000,000` is two numbers, not one; the three-digit groups say where."""

    report = parse_buyback_report(_default_filing())

    assert report.resolved_shares == 600_000
    assert report.resolved_amount_yen == 300_000_000


def test_a_filing_without_a_board_authorization_reports_nothing_rather_than_zero() -> None:
    """No board section means no authorization — not an exhausted one."""

    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            AcquisitionsByResolutionOfShareholdersMeetingTextBlock="該当事項はありません。",
            HoldingOfTreasurySharesTextBlock=_HOLDING,
        )
    )

    assert report.resolved_shares is None
    assert report.cumulative_shares is None
    assert report.remaining_shares is None
    assert report.consumed_ratio is None
    # The holding section is independent and still readable.
    assert report.issued_shares == 86_000_000


def test_an_unreadable_size_leaves_the_remainder_absent_instead_of_full() -> None:
    board = _BOARD_FULL_WIDTH.replace("600,000300,000,000", "－－")

    report = parse_buyback_report(_default_filing(board))

    assert report.resolved_shares is None
    assert report.cumulative_shares == 533_500
    assert report.remaining_shares is None


def test_a_reading_that_contradicts_the_form_is_discarded() -> None:
    """Cumulative above the authorized size means the labels latched onto the wrong row."""

    board = _BOARD_FULL_WIDTH.replace(
        "報告月末現在の累計取得自己株式533,500299,960,100",
        "報告月末現在の累計取得自己株式9,999,999299,960,100",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.resolved_shares is None
    assert report.cumulative_shares is None
    assert report.remaining_shares is None


def test_a_truncated_cumulative_below_the_month_is_discarded() -> None:
    """The split can end early and keep only the leading digits of a number.

    Truncation makes the value smaller, so every upper-bound check passes it: the store
    holds no row whose cumulative exceeds its authorization, yet 13 rows have a cumulative
    that fell below the previous month. The month's own acquisition is part of the
    cumulative, which is the relation a shrunken read breaks.
    """
    board = _BOARD_FULL_WIDTH.replace(
        "報告月末現在の累計取得自己株式533,500299,960,100",
        "報告月末現在の累計取得自己株式53299,960,100",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.cumulative_shares is None
    assert report.cumulative_amount_yen is None
    assert report.month_shares is None
    # The authorization row is read from a different label and survives.
    assert report.resolved_shares == 600_000


def test_a_cumulative_priced_above_any_real_share_is_discarded() -> None:
    """A share count that lost digits inflates the implied price per share.

    The store's real range runs to 68,184 yen; the only rows above it carry cumulative
    counts of 700 and 1 shares.
    """
    board = _BOARD_FULL_WIDTH.replace(
        "報告月末現在の累計取得自己株式533,500299,960,100",
        # 220,400 の月次を下回らない株数にして、単価だけが実在しない水準になる形にする。
        "報告月末現在の累計取得自己株式533,500299,960,100,000",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.cumulative_shares is None
    assert report.cumulative_amount_yen is None


def test_a_cumulative_amount_above_the_authorized_amount_is_discarded() -> None:
    board = _BOARD_FULL_WIDTH.replace(
        "報告月末現在の累計取得自己株式533,500299,960,100",
        "報告月末現在の累計取得自己株式533,500999,960,100",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.resolved_amount_yen is None
    assert report.cumulative_amount_yen is None


def test_a_window_that_ends_before_it_starts_is_discarded() -> None:
    board = _BOARD_FULL_WIDTH.replace(
        "取得期間　2026年５月11日～2026年７月31日",
        "取得期間　2026年７月31日～2026年５月11日",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.window_start is None
    assert report.window_end is None


def test_treasury_above_issued_is_discarded_as_a_misread() -> None:
    holding = _HOLDING.replace("保有自己株式数4,504,427", "保有自己株式数99,000,000")

    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock=_BOARD_FULL_WIDTH,
            HoldingOfTreasurySharesTextBlock=holding,
        )
    )

    assert report.issued_shares is None
    assert report.treasury_shares is None
    # The authorization side is independent and survives.
    assert report.resolved_shares == 600_000


def test_a_corrupt_archive_is_named_rather_than_silently_empty() -> None:
    with pytest.raises(BuybackReportError, match="readable archive"):
        parse_buyback_report(b"not a zip")


def test_an_empty_filing_reports_every_field_absent() -> None:
    report = parse_buyback_report(_filing(ReportingPeriodCoverPage=_PERIOD))

    assert report.report_month_end == date(2026, 7, 31)
    assert report.resolved_shares is None
    assert report.issued_shares is None
    assert report.remaining_shares is None
    assert report.disposition_observed is False


def test_disposition_purpose_requires_an_actual_positive_row() -> None:
    disposals = (
        "２【処理状況】2026年７月31日現在 区分"
        "引き受ける者の募集を行った取得自己株式（処分日）－月－日－－計－－－"
        "消却の処分を行った取得自己株式（消却日）－月－日－－計－－－"
        "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）"
        "－月－日－－計－－－"
        "その他（譲渡制限付株式報酬として処分した取得自己株式）（処分日）"
        "７月16日7,0003,766,000計－7,0003,766,000合計7,0003,766,000"
    )

    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock=_BOARD_FULL_WIDTH,
            HoldingOfTreasurySharesTextBlock=_HOLDING,
            DisposalsOfTreasurySharesTextBlock=disposals,
        )
    )

    assert report.disposition_observed is True
    assert report.disposition_purposes == ("employee_compensation_esop",)


def test_cancellation_and_other_rerelease_can_coexist() -> None:
    disposals = (
        "引き受ける者の募集を行った取得自己株式（処分日）６月２日1,000500,000計－1,000500,000"
        "消却の処分を行った取得自己株式（消却日）６月30日2,0001,000,000計－2,0001,000,000"
        "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）"
        "－月－日－－計－－－その他（該当事項なし）（処分日）－月－日－－計－－－"
        "合計3,0001,500,000"
    )

    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            DisposalsOfTreasurySharesTextBlock=disposals,
        )
    )

    assert report.disposition_purposes == ("cancellation", "other_rerelease")


def test_observed_empty_disposition_is_not_an_unknown_source() -> None:
    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            DisposalsOfTreasurySharesTextBlock="２【処理状況】該当事項はありません。",
        )
    )

    assert report.disposition_observed is True
    assert report.disposition_purposes == ()


@pytest.mark.parametrize(
    "disposals",
    [
        "２【処理状況】新設された処分類型（処分日）７月16日7,0003,766,000",
        (
            "引き受ける者の募集を行った取得自己株式（処分日）－月－日－－"
            "消却の処分を行った取得自己株式（消却日）－月－日－－"
        ),
        (
            "引き受ける者の募集を行った取得自己株式（処分日）－月－日－－"
            "消却の処分を行った取得自己株式（消却日）７月16日shares-unknown"
            "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）"
            "－月－日－－その他（該当事項なし）（処分日）－月－日－－合計－－"
        ),
    ],
    ids=("unknown-category", "truncated-table", "malformed-known-row"),
)
def test_unknown_or_malformed_disposition_layout_fails_closed(disposals: str) -> None:
    report = parse_buyback_report(
        _filing(
            ReportingPeriodCoverPage=_PERIOD,
            DisposalsOfTreasurySharesTextBlock=disposals,
        )
    )

    assert report.disposition_observed is False
    assert report.disposition_purposes == ()


def test_a_zero_sized_authorization_does_not_divide() -> None:
    board = _BOARD_FULL_WIDTH.replace("600,000300,000,000", "0300,000,000").replace(
        "報告月末現在の累計取得自己株式533,500299,960,100",
        "報告月末現在の累計取得自己株式0299,960,100",
    )

    report = parse_buyback_report(_default_filing(board))

    assert report.resolved_shares == 0
    assert report.consumed_ratio is None


def _csv_rows(content: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = archive.namelist()[0]
        return list(csv.reader(io.StringIO(archive.read(name).decode("utf-16")), delimiter="\t"))


def test_the_fixture_builder_matches_the_filing_shape_the_parser_expects() -> None:
    """Guards the tests themselves: a fixture that stopped resembling EDINET proves nothing."""

    rows = _csv_rows(_default_filing())

    assert all(row[0].startswith(_PREFIX) for row in rows)
    assert any(row[0].endswith("HoldingOfTreasurySharesTextBlock") for row in rows)
