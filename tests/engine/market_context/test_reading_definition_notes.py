"""The value's definition survives the public Reading projection without new fields."""

from datetime import date

import pytest

from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading.compute import compute_reading
from baibai_engine.market_context.reading.models import snapshot_payload
from baibai_engine.market_context.reading.observations import UnavailableObservationError
from baibai_engine.market_context.reading.rules import DEFAULT_RULES_PATH, load_reading_rules


@pytest.mark.parametrize("series_id", ["jp.n225_iv_term", "jp.n225_iv_skew"])
@pytest.mark.parametrize("state", ["observed", "empty", "unreadable"])
def test_option_definition_and_observation_notes_reach_reading_payload(series_id, state):
    definition = load_definitions().by_id()[series_id]
    assert definition.notes
    asof = date(2026, 10, 23)
    observation = ObservationRecord(
        series_id=series_id,
        observed_at=asof,
        value=1.5,
        unit=definition.unit,
        source_url=definition.source_url,
        notes=("source observation note",),
    )

    def reader(_series, _start, _end):
        if state == "unreadable":
            raise UnavailableObservationError("source unavailable")
        return (observation,) if state == "observed" else ()

    snapshot = compute_reading(
        series=(definition,),
        reader=reader,
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=asof,
    )
    payload = snapshot_payload(snapshot)["series"][0]
    assert payload["notes"][0] == f"Series definition: {definition.notes}"
    assert payload["latest_value"] == (1.5 if state == "observed" else None)
    if state == "observed":
        assert payload["notes"][1:] == ["source observation note"]
    elif state == "unreadable":
        assert payload["notes"][1:] == ["source unavailable"]
        assert payload["flags"] == ["source_unreadable"]
    else:
        assert len(payload["notes"]) == 1
