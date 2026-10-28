"""Missing published valuations supersede old values; both columns stay available."""

from contextlib import closing
from datetime import date

import pytest
from tests.helpers.network import _no_real_network as _no_real_network

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.nikkei.indices as nikkei_indexes
from baibai_engine.data_collection.receipts.native_sources import store as db
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)

DAY = date(2026, 7, 24)


def table(weighted, index_based):
    return (
        "<table><tr><th>Date</th><th>Weighted</th><th>Index</th></tr>"
        "<tr><!--daily_changing--><td>2026.07.24</td>"
        f"<!--daily_changing--><td>{weighted}</td>"
        f"<!--daily_changing--><td>{index_based}</td></tr></table>"
    )


@pytest.mark.parametrize("missing", ["-", "", "&mdash;"])
def test_missing_revision_does_not_restore_prior_native_value(tmp_path, monkeypatch, missing):
    path = tmp_path / "indicators.sqlite"
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.reading_key == "per"
    )
    replies = iter([table("17.82", "25.11"), table(missing, "25.12")])
    monkeypatch.setattr(nikkei_indexes, "fetch_text", lambda *args, **kwargs: next(replies))
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    first = service.get_range(series.series_id, start=DAY, end=DAY)
    assert first.observations[0].value == 17.82
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=DAY, end=DAY)
    assert corrected.observations == ()
    with closing(db.open_read_only_connection(path)) as connection:
        row = next(read_snapshots(connection, DAY, DAY, source_id=series.source_id)).records[0]
        assert row["weighted_text"] == ("—" if missing == "&mdash;" else missing)
        assert row["index_based_text"] == "25.12"


def test_missing_unused_index_column_does_not_drop_valid_weighted_value(tmp_path, monkeypatch):
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.reading_key == "per"
    )
    monkeypatch.setattr(nikkei_indexes, "fetch_text", lambda *args, **kwargs: table("17.82", "-"))
    path = tmp_path / "indicators.sqlite"
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    result = IndicatorsService(path).get_range(series.series_id, start=DAY, end=DAY)
    assert [row.value for row in result.observations] == [17.82]


def test_unreadable_numeric_text_is_retained_but_not_a_successful_reading(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.reading_key == "per"
    )
    monkeypatch.setattr(
        nikkei_indexes, "fetch_text", lambda *args, **kwargs: table("unreadable", "25")
    )
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with pytest.raises(ValueError, match="could not convert"):
        IndicatorsService(path).get_range(series.series_id, start=DAY, end=DAY)
    with closing(db.open_read_only_connection(path)) as connection:
        row = next(read_snapshots(connection, DAY, DAY, source_id=series.source_id)).records[0]
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
