"""Explicitly unknown current schedules must survive parsing and file merging."""

from datetime import date
from io import BytesIO

import pandas as pd
import pytest

from baibai_engine.market.jpx_decode import (
    build_earnings_calendar_snapshot,
    parse_earnings_calendar_excel,
)
from baibai_engine.storage.observations.ingest.jpx import store_jpx_earnings_calendar_snapshot
from baibai_engine.storage.observations.reader import read_jpx_earnings_calendar_snapshot


@pytest.mark.parametrize("schedule", ["", "未定_Undecided", "10月中旬", "conflicting"])
@pytest.mark.parametrize("reverse", [False, True])
def test_unknown_newer_schedule_blocks_older_known_date(tmp_path, schedule, reverse):
    prefix = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"

    def parse(name, stamp, rows):
        content = BytesIO()
        pd.DataFrame([[stamp, ""], ["決算発表予定日", "コード"], *rows]).to_excel(
            content, index=False, header=False
        )
        return parse_earnings_calendar_excel(content.getvalue(), prefix + name)

    new_rows = (
        [["2026-10-20", "130A"], ["2026-10-21", "130A"]]
        if schedule == "conflicting"
        else [[schedule, "130A"]]
    )
    newer = parse("kessan09_1015.xlsx", "2026年10月14日現在", new_rows)
    older = parse(
        "kessan08_1010.xlsx",
        "2026年10月9日現在",
        [["2026-10-20", "130A"], ["2026-10-22", "7203"]],
    )
    assert newer.entries == ()
    assert newer.undated_tickers == ("130A",)
    files = [older, newer] if reverse else [newer, older]
    by_url = {source.url: source for source in files}
    snapshot = build_earnings_calendar_snapshot(
        files, asof_date=date(2026, 10, 15), source_urls=tuple(by_url)
    )
    store_jpx_earnings_calendar_snapshot(tmp_path / "market.sqlite", snapshot)
    assert [entry.ticker for entry in snapshot.entries] == ["7203"]
    stored = read_jpx_earnings_calendar_snapshot(tmp_path / "market.sqlite", date(2026, 10, 15))
    assert stored is not None
    assert stored.entries == snapshot.entries
