"""The Macro indicator panel must map every registered series exactly once."""

from __future__ import annotations

from pathlib import Path

from baibai_app.sources.db_sources import load_macro_panel_config
from baibai_engine.macro.indicators.definitions import load_definitions

_PANEL_PATH = Path("method/macro-panel.yaml")


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
