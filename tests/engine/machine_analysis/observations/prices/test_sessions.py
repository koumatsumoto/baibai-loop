"""延伸前の終値を前日扱いせず、延伸後の未確定終値は使わない。"""

from datetime import date, datetime

import pytest
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date
from baibai_engine.portfolio.holdings.market_observations import read_unadjusted_close


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2024-11-01T14:59:59+09:00", "2024-10-31"),
        ("2024-11-01T15:00:00+09:00", "2024-11-01"),
        ("2024-11-01T15:29:59+09:00", "2024-11-01"),
        ("2024-11-01T06:00:00Z", "2024-11-01"),
        ("2024-11-05T15:00:00+09:00", "2024-11-04"),
        ("2024-11-05T15:29:59+09:00", "2024-11-04"),
        ("2024-11-05T15:30:00+09:00", "2024-11-05"),
        ("2024-11-05T06:30:00Z", "2024-11-05"),
    ],
)
def test_session_extension_boundary_uses_the_local_session_date(instant, expected):
    # This is a civil-date cutoff. The source calendar resolves holidays later.
    assert latest_eligible_close_date(datetime.fromisoformat(instant)) == date.fromisoformat(
        expected
    )


@pytest.mark.parametrize(
    ("day", "instant", "expected_day", "expected_close"),
    [
        ("2024-11-01", "2024-11-01T15:10:00+09:00", "2024-11-01", 120.0),
        ("2024-11-05", "2024-11-05T15:10:00+09:00", "2024-10-31", 100.0),
        ("2024-11-05", "2024-11-05T15:30:00+09:00", "2024-11-05", 120.0),
    ],
)
def test_raw_quote_replay_selects_the_session_close_without_replacing_its_source_clock(
    tmp_path, day, instant, expected_day, expected_close
):
    path = tmp_path / "market.sqlite"
    seed_daily_bars(path, [("1301", "2024-10-31", 100.0, 1.0), ("1301", day, 120.0, 1.0)])
    found = read_unadjusted_close(
        sqlite_path=path, ticker="1301", at=datetime.fromisoformat(instant)
    )
    assert found is not None
    assert (found.price_as_of, found.close_yen) == (
        date.fromisoformat(expected_day),
        expected_close,
    )
