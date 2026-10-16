"""Missing published valuations supersede old values; both columns stay available."""

from contextlib import closing
from datetime import date

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.providers import nikkei_indexes
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots

DAY = date(2026, 7, 24)


def table(weighted, index_based):
    return (
        "<table><tr><th>Date</th><th>Weighted</th><th>Index</th></tr>"
        "<tr><!--daily_changing--><td>2026.07.24</td>"
        f"<!--daily_changing--><td>{weighted}</td>"
        f"<!--daily_changing--><td>{index_based}</td></tr></table>"
    )


@pytest.mark.parametrize("missing", ["-", "", "&mdash;"])
def test_missing_revision_does_not_restore_scalar_or_prior_native_value(
    tmp_path, monkeypatch, missing
):
    path = tmp_path / "indicators.sqlite"
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.provider_series_id == "per"
    )
    replies = iter([table("17.82", "25.11"), table(missing, "25.12")])
    monkeypatch.setattr(nikkei_indexes, "fetch_text", lambda *args, **kwargs: next(replies))
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, DAY, 19.0)])
    service = IndicatorsService(path)
    first = service.get_range(series.series_id, start=DAY, end=DAY, refresh=True)
    assert first.observations[0].value == 17.82
    corrected = service.get_range(series.series_id, start=DAY, end=DAY, refresh=True)
    assert corrected.observations == ()
    with closing(db.open_read_only_connection(path)) as connection:
        row = next(
            read_snapshots(connection, DAY, DAY, source_key=native_source_key(series))
        ).records[0]
        assert row["weighted_text"] == ("—" if missing == "&mdash;" else missing)
        assert row["index_based_text"] == "25.12"
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 19.0


def test_missing_unused_index_column_does_not_drop_valid_weighted_value(tmp_path, monkeypatch):
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.provider_series_id == "per"
    )
    monkeypatch.setattr(nikkei_indexes, "fetch_text", lambda *args, **kwargs: table("17.82", "-"))
    result = IndicatorsService(tmp_path / "indicators.sqlite").get_range(
        series.series_id, start=DAY, end=DAY, refresh=True
    )
    assert [row.value for row in result.observations] == [17.82]


def test_unreadable_numeric_text_is_retained_but_not_a_successful_reading(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.provider_series_id == "per"
    )
    monkeypatch.setattr(
        nikkei_indexes, "fetch_text", lambda *args, **kwargs: table("unreadable", "25")
    )
    with pytest.raises(ValueError, match="could not convert"):
        IndicatorsService(path).get_range(series.series_id, start=DAY, end=DAY, refresh=True)
    with closing(db.open_read_only_connection(path)) as connection:
        row = next(
            read_snapshots(connection, DAY, DAY, source_key=native_source_key(series))
        ).records[0]
        assert row["weighted_text"] == "unreadable"


@pytest.mark.parametrize(
    "html",
    [
        "<html>challenge</html>",
        "<table></table>",
        "<table><tr><td>Unexpected layout</td></tr></table>",
    ],
)
def test_unrecognized_empty_html_does_not_claim_successful_empty_history(html):
    series = next(item for item in load_definitions().series if item.provider == "nikkei_indexes")
    with pytest.raises(nikkei_indexes.IndicatorsProviderError, match="data rows missing"):
        nikkei_indexes.parse_nikkei_valuation(series, html, start=DAY, end=DAY)
