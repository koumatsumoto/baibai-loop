"""The Macro indicator panel maps every registered series exactly once, and its view
tolerates a store that has not caught up with the registry."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
)
from baibai_engine.macro.indicators.definitions import IndicatorDefinitions, load_definitions
from baibai_engine.read_api import MACRO_READING_RULES_PATH
from baibai_web.readmodel.macro import build_macro, build_macro_series
from baibai_web.sources.db_sources import DbMacroSource, load_macro_panel_config
from baibai_web.sources.types import MacroGroupConfig, MacroSeriesConfig

_PANEL_PATH = Path("web/config/macro-panel.yaml")
_AS_OF = date(2026, 7, 25)


def _configured_series_ids() -> list[str]:
    groups = load_macro_panel_config(_PANEL_PATH)
    return [series.series_id for group in groups for series in group.series]


def test_panel_covers_every_registered_series_exactly_once() -> None:
    configured = _configured_series_ids()
    registered = {series.series_id for series in load_definitions().series}

    assert set(configured) == registered, {
        "missing_from_panel": sorted(registered - set(configured)),
        "unknown_in_panel": sorted(set(configured) - registered),
    }
    assert len(configured) == len(set(configured)), "a series is listed in more than one group"


def test_panel_groups_are_non_empty() -> None:
    groups = load_macro_panel_config(_PANEL_PATH)
    assert groups
    for group in groups:
        assert group.series, f"empty macro panel group: {group.title}"


def _panel_source(tmp_path: Path, *, series_ids: tuple[str, ...], stored: str) -> DbMacroSource:
    store = tmp_path / "macro.sqlite"
    stored_definition = load_definitions().by_id()[stored]
    connection = initialize_database(
        store,
        definitions=IndicatorDefinitions(series=(stored_definition,)),
    )
    try:
        # This is how a store written before the registry grew looks: it has no
        # row at all for the series added since.
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id=stored,
                    observed_at=date(2026, 7, 15),
                    value=4.3,
                    unit=stored_definition.unit,
                    source_url="https://example.com/series",
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()
    groups = (
        MacroGroupConfig(
            title="金利",
            series=tuple(MacroSeriesConfig(series_id=item, label=None) for item in series_ids),
        ),
    )
    # No application store, so the report index is empty and only the panel is exercised.
    return DbMacroSource(tmp_path / "absent.sqlite", store, groups, MACRO_READING_RULES_PATH)


def test_panel_keeps_a_configured_series_the_store_has_not_fetched(tmp_path: Path) -> None:
    source = _panel_source(tmp_path, series_ids=("us.10y", "jp.2y"), stored="us.10y")

    view = build_macro(source, as_of=_AS_OF)

    rows = {series.series_id: series for group in view.groups for series in group.series}
    assert list(rows) == ["us.10y", "jp.2y"]
    # The overview carries only a compact monthly sparkline, not full history.
    assert [point.observed_at for point in rows["us.10y"].points] == [date(2026, 7, 15)]
    # The row survives with no observations and keeps its registry name, so a store that
    # lags the registry shows the gap instead of dropping the series or failing the view.
    assert rows["jp.2y"].points == []
    definition = load_definitions().by_id()["jp.2y"]
    assert rows["jp.2y"].name == definition.name
    assert rows["jp.2y"].unit == definition.unit
    history = build_macro_series(source, series_id="us.10y", as_of=_AS_OF)
    assert history is not None
    assert history.points


def test_panel_sparkline_uses_month_end_observations_and_is_bounded(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    definition = load_definitions().by_id()["us.10y"]
    connection = initialize_database(
        store,
        definitions=IndicatorDefinitions(series=(definition,)),
    )
    try:
        observations: list[ObservationRecord] = []
        # Fifteen calendar months with two observations each.  The overview must retain
        # only the final observation in each month and at most thirteen months.
        for offset in range(15):
            month_index = 2023 * 12 + 11 + offset  # December 2023 is zero-based month 11.
            year, zero_based_month = divmod(month_index, 12)
            month = zero_based_month + 1
            for day in (1, monthrange(year, month)[1]):
                observations.append(
                    ObservationRecord(
                        series_id="us.10y",
                        observed_at=date(year, month, day),
                        value=3.0 + offset / 100 + day / 1000,
                        unit=definition.unit,
                        source_url="https://example.com/series",
                    )
                )
        insert_observations(connection, observations)
        connection.commit()
    finally:
        connection.close()
    groups = (
        MacroGroupConfig(
            title="金利・金融政策",
            series=(MacroSeriesConfig(series_id="us.10y", label=None),),
        ),
    )
    source = DbMacroSource(
        tmp_path / "absent.sqlite",
        store,
        groups,
        MACRO_READING_RULES_PATH,
    )

    as_of = date(2025, 2, 28)
    view = build_macro(source, as_of=as_of)

    points = view.groups[0].series[0].points
    assert len(points) == 13
    assert points[0].observed_at == date(2024, 2, 29)
    assert points[-1].observed_at == as_of
    assert all(
        point.observed_at.day == monthrange(point.observed_at.year, point.observed_at.month)[1]
        for point in points
    )
    history = build_macro_series(source, series_id="us.10y", as_of=as_of)
    assert history is not None
    assert len(history.points) == 30


def test_panel_rejects_a_configured_series_no_registry_defines(tmp_path: Path) -> None:
    source = _panel_source(tmp_path, series_ids=("us.10y", "jp.no_such_series"), stored="us.10y")

    with pytest.raises(ValueError, match=r"not registered: jp\.no_such_series"):
        build_macro(source, as_of=_AS_OF)
