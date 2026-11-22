"""Missing published valuations supersede old values; both columns stay available."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest import TestCase

import pytest

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.nikkei.indices as nikkei_indexes
from baibai_engine.data_collection.providers.nikkei.indices import parse_nikkei_valuation
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
    with pytest.raises(nikkei_indexes.IndicatorsProviderError, match="data rows missing"):
        parse_nikkei_valuation(html, year=DAY.year, month=DAY.month)


@pytest.mark.parametrize(("year", "month"), [(2025, 7), (2026, 8)])
def test_parser_checks_requested_year_and_month(year, month):
    with pytest.raises(nikkei_indexes.IndicatorsProviderError, match="outside requested month"):
        parse_nikkei_valuation(table("17.82", "25.11"), year=year, month=month)


def test_refresh_limits_valid_month_to_requested_interval(tmp_path, monkeypatch):
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "nikkei_indexes" and item.reading_key == "per"
    )
    reply = (
        "<table>"
        "<tr><td>2026.07.23</td><td>18.40</td><td>25.20</td></tr>"
        "<tr><td>2026.07.24</td><td>17.82</td><td>25.11</td></tr>"
        "<tr><td>2026.07.27</td><td>17.25</td><td>24.67</td></tr>"
        "</table>"
    )
    monkeypatch.setattr(nikkei_indexes, "fetch_text", lambda *args, **kwargs: reply)
    path = tmp_path / "indicators.sqlite"
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with closing(db.open_read_only_connection(path)) as connection:
        rows = list(
            read_snapshots(
                connection, date(2026, 7, 1), date(2026, 7, 31), source_id=series.source_id
            )
        )
        assert [row.observed_at for row in rows] == [DAY]
        assert rows[0].records[0]["weighted_text"] == "17.82"


class NativeParserTests(TestCase):
    def test_parse_nikkei_valuation_retains_both_published_columns(self) -> None:
        html = (
            "<table><tbody>"
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>17.82</td><!--daily_changing--><td>25.11</td></tr>"
            "</tbody></table>"
        )

        observations = parse_nikkei_valuation(html, year=2026, month=7)

        # Retain both columns; selecting the weighted average belongs to L2.
        self.assertEqual(
            observations,
            {
                date(2026, 7, 24): [
                    {
                        "date_text": "2026.07.24",
                        "weighted_text": "17.82",
                        "index_based_text": "25.11",
                    }
                ]
            },
        )

    def test_parse_nikkei_valuation_leaves_numeric_policy_to_l2(self) -> None:
        html = (
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>99.90</td><!--daily_changing--><td>120.0</td></tr>"
        )

        observations = parse_nikkei_valuation(html, year=2026, month=7)
        self.assertEqual(observations[date(2026, 7, 24)][0]["weighted_text"], "99.90")
