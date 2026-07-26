"""A published report's own invalidation conditions, checked against the L1 history."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from baibai_engine.macro.context.cli import main as context_main
from baibai_engine.macro.context.diagnostics import (
    macro_context_diagnostics,
    macro_context_from_payload,
)
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.context.triggers import (
    TriggerEvaluationError,
    evaluate_triggers,
    fired_trigger_summaries,
)
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    get_series,
    initialize_database,
    insert_observations,
)
from tests.helpers.macro_context import macro_context_payload

_BREAKOUT = [{"series_id": "us.10y", "comparison": "at_or_above", "threshold": 5.0}]


def _document(machine_conditions: list[dict[str, Any]] | None = None) -> MacroContextDocument:
    return MacroContextDocument.model_validate(
        macro_context_payload(machine_conditions=machine_conditions)
    )


def _reader(observations: Sequence[ObservationRecord]):  # type: ignore[no-untyped-def]
    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return tuple(
            item
            for item in observations
            if item.series_id == series_id and start <= item.observed_at <= end
        )

    return read


def _observation(observed_at: date, value: float) -> ObservationRecord:
    return ObservationRecord(
        series_id="us.10y",
        observed_at=observed_at,
        value=value,
        unit="percent",
        source_url="https://www.federalreserve.gov/datadownload/Output.aspx",
        vintage_at=datetime.combine(observed_at, datetime.min.time(), tzinfo=UTC),
    )


def _evaluate(observations: Sequence[ObservationRecord], *, asof: date):  # type: ignore[no-untyped-def]
    return evaluate_triggers(
        _document(_BREAKOUT),
        reader=_reader(observations),
        known_series=frozenset({"us.10y"}),
        asof=asof,
    )


def test_a_condition_the_window_met_fires_with_the_first_matching_observation() -> None:
    evaluation = _evaluate(
        [
            _observation(date(2026, 7, 20), 4.8),
            _observation(date(2026, 7, 21), 5.1),
            _observation(date(2026, 7, 22), 5.4),
        ],
        asof=date(2026, 7, 22),
    )

    result = evaluation.results[0]
    assert result.status == "fired"
    assert result.observation is not None
    assert result.observation.observed_at == date(2026, 7, 21)
    assert len(evaluation.fired) == 1


def test_a_condition_no_observation_met_is_quiet_with_the_latest_observation() -> None:
    evaluation = _evaluate(
        [_observation(date(2026, 7, 20), 4.3), _observation(date(2026, 7, 21), 4.4)],
        asof=date(2026, 7, 21),
    )

    result = evaluation.results[0]
    assert result.status == "quiet"
    assert result.observation is not None
    assert result.observation.observed_at == date(2026, 7, 21)
    assert evaluation.fired == ()


def test_a_series_that_has_not_printed_inside_the_window_blocks_nothing() -> None:
    """Unlike a scorecard, a trigger is a prompt to look again, not a settlement."""

    evaluation = _evaluate([], asof=date(2026, 7, 21))

    result = evaluation.results[0]
    assert result.status == "not_evaluable"
    assert result.observation is None


def test_the_report_own_closing_observation_is_not_a_change_to_it() -> None:
    """The window opens the day after as_of; the report already read up to its as_of."""

    evaluation = _evaluate([_observation(date(2026, 7, 19), 5.4)], asof=date(2026, 7, 20))

    assert evaluation.results[0].status == "not_evaluable"


def test_an_observation_on_the_asof_day_is_inside_the_window() -> None:
    evaluation = _evaluate([_observation(date(2026, 7, 20), 5.4)], asof=date(2026, 7, 20))

    assert evaluation.results[0].status == "fired"


def test_a_series_the_registry_no_longer_defines_is_not_evaluable() -> None:
    evaluation = evaluate_triggers(
        _document(_BREAKOUT),
        reader=_reader([_observation(date(2026, 7, 20), 5.4)]),
        known_series=frozenset(),
        asof=date(2026, 7, 20),
    )

    assert evaluation.results[0].status == "not_evaluable"


def test_triggers_refuse_an_asof_before_the_report() -> None:
    with pytest.raises(TriggerEvaluationError, match="predates context as_of"):
        _evaluate([], asof=date(2026, 7, 18))


def test_a_report_without_machine_conditions_evaluates_to_nothing() -> None:
    """Every published report predates this field, so its absence must stay ordinary."""

    evaluation = evaluate_triggers(
        _document(),
        reader=_reader([]),
        known_series=frozenset({"us.10y"}),
        asof=date(2026, 7, 25),
    )

    assert evaluation.results == ()
    assert evaluation.fired == ()


def test_a_condition_on_a_series_the_section_does_not_cite_is_rejected() -> None:
    with pytest.raises(ValidationError, match="monitoring conditions must cite series"):
        _document([{"series_id": "jp.10y", "comparison": "above", "threshold": 2.0}])


def test_the_same_condition_written_twice_is_rejected() -> None:
    with pytest.raises(ValidationError, match="monitoring conditions must differ"):
        _document(_BREAKOUT * 2)


def test_selection_warns_when_a_report_stated_condition_has_been_met() -> None:
    context = macro_context_from_payload(
        _document(_BREAKOUT).payload(),
        source="fixture.yaml",
    )
    fired = replace(
        context,
        fired_triggers=("米国債市場: us.10y at_or_above 5 (2026-07-21 = 5.1)",),
    )

    quiet_diagnostics = macro_context_diagnostics(context, asof_date=date(2026, 7, 22))
    fired_diagnostics = macro_context_diagnostics(fired, asof_date=date(2026, 7, 22))

    assert "macro_context_invalidated" not in quiet_diagnostics["warnings"]
    assert "macro_context_invalidated" in fired_diagnostics["warnings"]
    assert fired_diagnostics["fired_triggers"] == list(fired.fired_triggers)


def test_fired_trigger_summaries_report_nothing_without_an_indicator_store(
    tmp_path: Path,
) -> None:
    application_db = tmp_path / "app.sqlite"
    document = _document(_BREAKOUT)
    MacroContextService(application_db).publish(document, expected_head=None)

    assert (
        fired_trigger_summaries(
            context_db=application_db,
            indicators_db_path=tmp_path / "absent.sqlite",
            context_id=document.context_id,
            asof=date(2026, 7, 22),
        )
        == ()
    )


def test_triggers_read_the_stores_end_to_end(tmp_path: Path) -> None:
    application_db = tmp_path / "app.sqlite"
    indicators_db = tmp_path / "macro.sqlite"
    document = _document(_BREAKOUT)
    MacroContextService(application_db).publish(document, expected_head=None)
    connection = initialize_database(indicators_db)
    try:
        series = get_series(connection, "us.10y")
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 7, 21),
                    value=5.1,
                    unit=series.unit,
                    source_url=series.source_url,
                    vintage_at=datetime(2026, 7, 21, tzinfo=UTC),
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()

    summaries = fired_trigger_summaries(
        context_db=application_db,
        indicators_db_path=indicators_db,
        context_id=document.context_id,
        asof=date(2026, 7, 22),
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = context_main(
            [
                "--db",
                str(application_db),
                "triggers",
                "--context-id",
                document.context_id,
                "--asof",
                "2026-07-22",
                "--indicators-db",
                str(indicators_db),
                "--format",
                "json",
            ]
        )

    payload = json.loads(stdout.getvalue())
    assert summaries == ("米国債市場: us.10y at_or_above 5 (2026-07-21 = 5.1)",)
    assert exit_code == 0
    assert payload["kind"] == "macro-context-triggers"
    assert [item["status"] for item in payload["results"]] == ["fired"]
