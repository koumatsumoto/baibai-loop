from datetime import UTC, date, datetime

from baibai_jobs.publication.snapshot_publication import _publication_day


def test_default_publication_day_uses_japan_date_at_utc_boundary():
    assert _publication_day(None, datetime(2026, 11, 8, 15, 1, tzinfo=UTC)) == date(2026, 11, 9)
    assert _publication_day(None, datetime(2026, 11, 8, 14, 59, tzinfo=UTC)) == date(2026, 11, 8)


def test_explicit_publication_day_takes_precedence():
    assert _publication_day(date(2026, 11, 7), datetime(2026, 11, 8, 15, 1, tzinfo=UTC)) == date(
        2026, 11, 7
    )
