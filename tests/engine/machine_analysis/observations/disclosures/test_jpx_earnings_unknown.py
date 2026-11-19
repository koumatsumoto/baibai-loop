"""Explicitly unknown current schedules must survive parsing and file merging."""

from datetime import date

import pytest
from tests.helpers.jpx_earnings import _workbook

from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    build_earnings_calendar_snapshot,
    parse_earnings_calendar_excel,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import (
    read_jpx_earnings_calendar_snapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
)


@pytest.mark.parametrize("schedule", ["", "未定_Undecided", "10月中旬", "conflicting"])
def test_unknown_newer_schedule_blocks_older_known_date(tmp_path, schedule, subtests):
    prefix = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"

    new_rows = (
        [["2026-10-20", "130A"], ["2026-10-21", "130A"]]
        if schedule == "conflicting"
        else [[schedule, "130A"]]
    )
    newer = parse_earnings_calendar_excel(
        _workbook("2026年10月14日現在", new_rows), prefix + "kessan09_1015.xlsx"
    )
    older = parse_earnings_calendar_excel(
        _workbook("2026年10月9日現在", [["2026-10-20", "130A"], ["2026-10-22", "7203"]]),
        prefix + "kessan08_1010.xlsx",
    )
    assert newer.entries == ()
    assert newer.undated_tickers == ("130A",)
    day = date(2026, 10, 15)
    for order, files in (("new-first", (newer, older)), ("old-first", (older, newer))):
        with subtests.test(order=order):
            db = tmp_path / f"{order}.sqlite"
            snapshot = build_earnings_calendar_snapshot(
                files, asof_date=day, source_urls=tuple(source.url for source in files)
            )
            store_jpx_earnings_calendar_snapshot(
                db, snapshot, fetched_at_utc="2026-10-15T00:00:00+00:00"
            )
            assert [(entry.ticker, entry.announcement_date) for entry in snapshot.entries] == [
                ("7203", date(2026, 10, 22))
            ]
            stored = read_jpx_earnings_calendar_snapshot(db, day)
            assert stored is not None
            assert stored.entries == snapshot.entries
