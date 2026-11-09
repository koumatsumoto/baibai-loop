from datetime import UTC, date, datetime

import pytest

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
    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            hour, minute = (14, 59) if cls.calls == 1 else (15, 1)
            return datetime(2026, 11, 8, hour, minute, tzinfo=UTC).astimezone(tz)

    class Owner:
        def __init__(self, *args):
            pass

        def fixed(self):
            return object()

    seen = []

    def capture(*args, as_of, **kwargs):
        seen.append(as_of)
        if len(seen) == 3:
            raise RuntimeError("stop after native inputs")
        return object()

    monkeypatch.setattr(publication, "datetime", Clock)
    for name in ("CalendarL1", "MasterL1", "PriceL1"):
        monkeypatch.setattr(publication, name, Owner)
    for name in (
        "native_calendar_model_input",
        "native_master_model_input",
        "native_price_model_input",
    ):
        monkeypatch.setattr(publication, name, capture)
    with (
        pytest.raises(RuntimeError, match="stop after native inputs"),
        publication.prepare_market_lake(
            sqlite_path=tmp_path / "x", mirror_root=tmp_path, store=object()
        ),
    ):
        pass
    assert seen == [date(2026, 11, 8)] * 3
