from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from baibai_engine.collection.economic.acquisition import (
    SourceRefreshFailure,
    SourceRefreshSuccess,
    refresh_sources,
)
from baibai_engine.collection.economic.refresh_policy import (
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
    source_policy,
)
from baibai_engine.collection.economic.sources import load_sources
from baibai_engine.collection.economic.store import initialize_database, record_source_run

END = date(2026, 10, 23)


def attempt(conn, start, end, status="ok"):
    record_source_run(
        conn,
        source_id="fred-payems",
        start=start,
        end=end,
        started_at=datetime(2026, 10, 23, tzinfo=UTC),
        status=status,
        record_count=0,
    )


def test_windows_preserve_revision_and_publication_lag_without_expanding_daily_options():
    catalog = load_sources()
    for source_id in ("fred-payems", "fred-a191rl1q225sbea", "boj-tankan-lmfg"):
        policy = source_policy(catalog[source_id])
        assert policy.lookback_days == 370
        assert END - timedelta(days=policy.lookback_days) < date(2026, 7, 1)
    assert source_policy(catalog["jquants-options-n225"]).lookback_days == 14
    assert source_policy(catalog["jquants-trades-spec"]).lookback_days == 60
    assert source_policy(catalog["fred-payems"]).mode == "bulk_then_filter"
    assert source_policy(catalog["matsui-margin"]).mode == "current_only"
    assert {
        source_policy(s).acquisition_group
        for s in catalog.values()
        if s.provider.startswith("jquants_")
    } == {"jquants"}


def test_recovery_does_not_hide_a_failed_old_range_behind_a_narrow_new_success(tmp_path):
    source = load_sources()["fred-payems"]
    with closing(initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        attempt(conn, date(2024, 1, 1), date(2024, 2, 1), "failed")
        attempt(conn, date(2026, 9, 1), END)
        assert recovery_ranges(conn, source, END)[0][0] == date(2024, 1, 1)


def test_recovery_uses_contiguous_success_ranges_and_finds_internal_gaps(tmp_path):
    source = load_sources()["fred-payems"]
    with closing(initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        attempt(conn, date(2024, 1, 1), date(2024, 2, 1))
        attempt(conn, date(2024, 3, 1), END)
        assert recovery_ranges(conn, source, END) == ((date(2024, 2, 2), date(2024, 2, 29)),)
        attempt(conn, date(2024, 1, 15), date(2024, 3, 3))
        assert recent_start(source, END) == END - timedelta(days=370)
        assert recovery_ranges(conn, source, END) == ()


def test_first_recent_refresh_is_distinct_from_explicit_history_rebuild(tmp_path, monkeypatch):
    from baibai_engine.collection.economic import acquisition

    starts = []

    def fetch(source, *, start, **kwargs):
        starts.append(start)
        return {}

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    path = tmp_path / "source.sqlite"
    result = refresh_sources(["fred-payems"], db_path=path, start=None, end=END, recent=True)
    assert isinstance(result[0], SourceRefreshSuccess)
    assert starts == [END - timedelta(days=370)]
    refresh_sources(["fred-payems"], db_path=path, start=None, end=END)
    assert starts[-1] == date(1900, 1, 1)


def test_current_page_cannot_claim_a_historical_recovery(tmp_path, monkeypatch):
    from baibai_engine.collection.economic import acquisition

    monkeypatch.setattr(
        acquisition,
        "fetch_source_documents",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("historical page fetched")),
    )
    outcomes = refresh_sources(
        ["matsui-margin"],
        db_path=tmp_path / "source.sqlite",
        start=None,
        end=date(2000, 1, 1),
        recent=True,
    )
    assert isinstance(outcomes[0], SourceRefreshFailure)
    assert "current-only" in outcomes[0].message


def test_failed_all_history_attempt_does_not_reopen_itself_during_recent_refresh(tmp_path):
    from baibai_engine.collection.economic.records import store_snapshot

    source = load_sources()["fred-payems"]
    with closing(initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        attempt(conn, date(1900, 1, 1), END, "failed")
        store_snapshot(
            conn,
            source_id=source.source_id,
            observed_at=date(1900, 1, 1),
            fetched_at=datetime(2026, 10, 23, tzinfo=UTC),
            records=[],
        )
        assert recent_start(source, END) == END - timedelta(days=370)
        assert recovery_ranges(conn, source, END) == ()
        assert conn.execute("SELECT status FROM economic_source_runs").fetchone()[0] == "failed"
        assert explicit_recovery_ranges(conn, source, END)


def test_partially_received_native_days_are_not_reacquired_as_missing_history(tmp_path):
    from baibai_engine.collection.economic.records import store_snapshot

    source = load_sources()["fred-payems"]
    with closing(initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        attempt(conn, date(2024, 1, 1), date(2024, 1, 31))
        attempt(conn, date(2024, 3, 1), END)
        store_snapshot(
            conn,
            source_id=source.source_id,
            observed_at=date(2024, 2, 1),
            fetched_at=datetime(2026, 10, 23, tzinfo=UTC),
            records=[],
        )
        assert recovery_ranges(conn, source, END) == ((date(2024, 2, 2), date(2024, 2, 29)),)


def test_pmi_partial_pdf_months_cover_history_even_when_the_range_failed(tmp_path):
    from baibai_engine.collection.economic.documents import SourceDocument, store_document
    source = load_sources()["spglobal-jp-pmi-mfg"]
    with closing(initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        record_source_run(conn, source_id=source.source_id, start=date(2025, 1, 1), end=date(2025, 10, 1), started_at=datetime(2025, 10, 1, tzinfo=UTC), status="failed", record_count=9)
        for month in range(1, 10):
            day = date(2025, month, 1)
            store_document(conn, SourceDocument(source.source_id, day.isoformat(), "https://example.com/release.pdf", "application/pdf", datetime(2025, 10, 1, tzinfo=UTC), b"%PDF original", {"observed_at": day.isoformat()}))
        assert recovery_ranges(conn, source, END) == ((date(2025, 10, 1), date(2025, 10, 17)),)
