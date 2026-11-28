from datetime import UTC, date, datetime

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.snapshot_publication import _publication_day


def test_default_publication_day_uses_japan_date_at_utc_boundary():
    assert _publication_day(None, datetime(2026, 11, 8, 15, 1, tzinfo=UTC)) == date(2026, 11, 9)
    assert _publication_day(None, datetime(2026, 11, 8, 14, 59, tzinfo=UTC)) == date(2026, 11, 8)


def test_explicit_publication_day_takes_precedence():
    assert _publication_day(date(2026, 11, 7), datetime(2026, 11, 8, 15, 1, tzinfo=UTC)) == date(
        2026, 11, 7
    )


def test_prepare_uses_one_day_even_when_japan_midnight_passes(tmp_path, monkeypatch):
    before_midnight = datetime(2026, 11, 8, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 11, 8, 15, 1, tzinfo=UTC)

    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return (before_midnight if cls.calls <= 2 else after_midnight).astimezone(tz)

    class Owner:
        def __init__(self, *args):
            pass

        def fixed(self):
            return object()

    seen = []
    calendar = object()

    def capture_calendar(*args, as_of, evaluated_at, **kwargs):
        seen.append(("calendar", as_of, evaluated_at))
        return calendar

    def capture_price(*args, as_of, evaluated_at, calendar_input, **kwargs):
        assert calendar_input is calendar
        seen.append(("price", as_of, evaluated_at))
        raise RuntimeError("stop after native inputs")

    monkeypatch.setattr(publication, "datetime", Clock)
    monkeypatch.setattr(publication, "CalendarL1", Owner)
    monkeypatch.setattr(publication, "PriceL1", Owner)
    monkeypatch.setattr(publication, "native_calendar_model_input", capture_calendar)
    monkeypatch.setattr(publication, "native_price_model_input", capture_price)
    with (
        pytest.raises(RuntimeError, match="stop after native inputs"),
        publication.prepare_market_lake(
            sqlite_path=tmp_path / "x", mirror_root=tmp_path, store=MemoryR2Store()
        ),
    ):
        pass
    assert seen == [
        ("calendar", date(2026, 11, 8), before_midnight),
        ("price", date(2026, 11, 8), after_midnight),
    ]
