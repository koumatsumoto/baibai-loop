from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.macro.context.cli import main as context_main
from baibai_engine.macro.context.models import (
    MacroContextDocument,
    scorecard_snapshot_input_id,
)
from baibai_engine.macro.context.scorecard import (
    ScorecardEvaluation,
    ScorecardEvaluationError,
    ScorecardResult,
    ScorecardStores,
    _has_provider_run,
    _latest_vintages,
    _matches,
    evaluate_scorecard,
)
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
    record_provider_run,
)
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.reader import build_store_observation_reader
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH, rules_revision


def _document(*, deadline: str = "2026-10-31") -> MacroContextDocument:
    return MacroContextDocument.model_validate(macro_context_payload(scorecard_deadline=deadline))


def _observations(*points: tuple[date, float]) -> tuple[ObservationRecord, ...]:
    return tuple(
        ObservationRecord(
            series_id="us.10y",
            observed_at=observed_at,
            value=value,
            unit="percent",
            source_url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
            vintage_at=datetime.combine(observed_at, datetime.min.time(), tzinfo=UTC),
        )
        for observed_at, value in points
    )


def _reader(observations: Sequence[ObservationRecord]):  # type: ignore[no-untyped-def]
    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return tuple(
            observation
            for observation in observations
            if observation.series_id == series_id and start <= observation.observed_at <= end
        )

    return read


def _result(
    evaluation: ScorecardEvaluation,
    *,
    case: Literal["base", "bear", "bull"],
    condition_index: int,
) -> ScorecardResult:
    return next(
        item
        for item in evaluation.results
        if item.case == case and item.condition_index == condition_index
    )


def _foreign_flow_document() -> MacroContextDocument:
    payload = macro_context_payload(scorecard_deadline="2026-08-23")
    payload["inputs"]["indicator_series"][0]["series_id"] = "jp.foreign_flows"
    # Substitute rather than collapse: the fixture's second series keeps each dominant
    # force backed by a distinct series per named section.
    for force in payload["synthesis"]["dominant_forces"]:
        force["series_ids"] = [
            "jp.foreign_flows" if listed == "us.10y" else listed for listed in force["series_ids"]
        ]
    for section in payload["core"]:
        section["series_ids"] = [
            "jp.foreign_flows" if listed == "us.10y" else listed for listed in section["series_ids"]
        ]
        for scenario in section["scenarios"]:
            for condition in scenario["scorecard"]:
                condition["series_id"] = "jp.foreign_flows"
        for point in section["monitoring_points"]:
            for condition in point.get("machine_conditions", []):
                condition["series_id"] = "jp.foreign_flows"
    payload["connection"]["series_ids"] = ["jp.foreign_flows"]
    return MacroContextDocument.model_validate(payload)


def _evaluate(
    observations: Sequence[ObservationRecord],
    *,
    asof: date,
    accessed_at: datetime,
    settlement: bool = True,
    staleness_warn_days: int = 1,
    providers: dict[str, str] | None = None,
) -> ScorecardEvaluation:
    return evaluate_scorecard(
        _document(),
        reader=_reader(observations),
        provider_run_checker=(
            lambda _series_id, _provider, _start, _end, _completed_after, _asof: settlement
        ),
        providers={"us.10y": "fred_csv"} if providers is None else providers,
        stale_after=lambda _series_id, observed_at: (
            observed_at + timedelta(days=staleness_warn_days)
        ),
        rules_revision="test-rules",
        asof=asof,
        accessed_at=accessed_at,
        command="baibai-engine macro context scorecard --fixture",
        stores=ScorecardStores(
            context_db=str(Path("fixture-app.sqlite").resolve()),
            indicators_db=str(Path("fixture-macro.sqlite").resolve()),
        ),
    )


def test_scorecard_marks_first_match_met_and_unsettled_conditions_pending() -> None:
    evaluation = _evaluate(
        _observations(
            (date(2026, 7, 21), 4.5),
            (date(2026, 7, 20), 4.0),
        ),
        asof=date(2026, 7, 20),
        accessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    base = _result(evaluation, case="base", condition_index=1)
    bear = _result(evaluation, case="bear", condition_index=1)
    assert base.status == "met"
    assert base.observation is not None
    assert (base.observation.observed_at, base.observation.value) == (
        date(2026, 7, 20),
        4.0,
    )
    assert bear.status == "pending"
    assert bear.observation is not None
    assert bear.observation.observed_at == date(2026, 7, 20)


@pytest.mark.parametrize(
    ("comparison", "expected"),
    [
        ("below", False),
        ("at_or_below", True),
        ("above", False),
        ("at_or_above", True),
    ],
)
def test_scorecard_comparison_boundaries(
    comparison: Literal["below", "at_or_below", "above", "at_or_above"],
    expected: bool,
) -> None:
    assert (
        _matches(
            4.0,
            comparison=comparison,
            threshold=4.0,
        )
        is expected
    )


def test_scorecard_does_not_count_a_match_at_the_context_baseline() -> None:
    evaluation = _evaluate(
        _observations((date(2026, 7, 19), 5.5)),
        asof=date(2026, 7, 20),
        accessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    bear = _result(evaluation, case="bear", condition_index=1)
    assert bear.status == "pending"
    assert bear.observation is None
    assert evaluation.machine_snapshot.observation_as_of is None


def test_generic_machine_snapshot_cannot_omit_observation_date() -> None:
    payload = macro_context_payload()
    payload["inputs"]["machine_snapshots"][0]["observation_as_of"] = None

    with pytest.raises(ValueError, match="valid date"):
        MacroContextDocument.model_validate(payload)


def test_scorecard_accepts_a_matching_observation_on_the_deadline() -> None:
    evaluation = _evaluate(
        _observations(
            (date(2026, 10, 30), 4.0),
            (date(2026, 10, 31), 2.9),
        ),
        asof=date(2026, 10, 31),
        accessed_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )

    bull = _result(evaluation, case="bull", condition_index=1)
    bear = _result(evaluation, case="bear", condition_index=1)
    assert bull.status == "met"
    assert bull.observation is not None
    assert bull.observation.observed_at == date(2026, 10, 31)
    assert bear.status == "pending"


def test_scorecard_excludes_a_first_match_after_the_deadline() -> None:
    evaluation = _evaluate(
        _observations(
            (date(2026, 10, 31), 4.0),
            (date(2026, 11, 1), 5.5),
        ),
        asof=date(2026, 11, 1),
        accessed_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
    )

    bear = _result(evaluation, case="bear", condition_index=1)
    assert bear.status == "not_met"
    assert bear.observation is not None
    assert (bear.observation.observed_at, bear.observation.value) == (
        date(2026, 10, 31),
        4.0,
    )


def test_scorecard_keeps_pending_until_the_settlement_watermark() -> None:
    evaluation = _evaluate(
        _observations((date(2026, 10, 31), 4.0)),
        asof=date(2026, 11, 1),
        accessed_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
        staleness_warn_days=7,
    )

    bear = _result(evaluation, case="bear", condition_index=1)
    assert bear.status == "pending"
    assert bear.settlement_ready_on == date(2026, 11, 7)


def test_scorecard_selects_the_latest_vintage_independently_of_input_order() -> None:
    older = _observations((date(2026, 7, 20), 4.1))[0]
    newer = _observations((date(2026, 7, 20), 4.9))[0]
    older = replace(older, vintage_at=datetime(2026, 7, 21, tzinfo=UTC))
    newer = replace(newer, vintage_at=datetime(2026, 7, 22, tzinfo=UTC))

    assert _latest_vintages((newer, older)) == (newer,)
    assert _latest_vintages((older, newer)) == (newer,)


def test_scorecard_rejects_conflicting_observations_with_the_same_identity() -> None:
    first = _observations((date(2026, 7, 20), 4.1))[0]
    second = _observations((date(2026, 7, 20), 4.9))[0]
    vintage = datetime(2026, 7, 22, tzinfo=UTC)
    first = replace(first, vintage_at=vintage)
    second = replace(second, vintage_at=vintage)

    with pytest.raises(ScorecardEvaluationError, match="share an identity"):
        _latest_vintages((first, second))


def test_scorecard_refuses_to_settle_an_expired_condition_without_an_observation() -> None:
    with pytest.raises(
        ScorecardEvaluationError,
        match="cannot settle expired scorecard condition without an observation",
    ):
        _evaluate(
            (),
            asof=date(2026, 11, 1),
            accessed_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
        )


def test_scorecard_rejects_a_future_asof() -> None:
    with pytest.raises(ScorecardEvaluationError, match="is in the future"):
        _evaluate(
            _observations((date(2026, 7, 20), 4.0)),
            asof=date(2026, 7, 21),
            accessed_at=datetime(2026, 7, 20, 14, 59, tzinfo=UTC),
        )


def test_scorecard_refuses_to_settle_a_condition_on_a_retired_series() -> None:
    """The report stays readable after a retirement; its scorecard becomes unsettleable."""

    with pytest.raises(ScorecardEvaluationError, match=re.escape("no longer defines: us.10y")):
        _evaluate(
            _observations((date(2026, 7, 20), 4.0)),
            asof=date(2026, 7, 20),
            accessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
            providers={},
        )


def test_scorecard_refuses_to_mark_met_without_observation_vintage() -> None:
    observation = replace(
        _observations((date(2026, 7, 20), 4.0))[0],
        vintage_at=None,
    )
    with pytest.raises(
        ScorecardEvaluationError,
        match="cannot prove first scorecard match without vintage_at",
    ):
        _evaluate(
            (observation,),
            asof=date(2026, 7, 20),
            accessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )


def test_scorecard_refuses_to_mark_met_without_a_full_window_provider_run() -> None:
    with pytest.raises(
        ScorecardEvaluationError,
        match="without an eligible successful provider run",
    ):
        _evaluate(
            _observations((date(2026, 7, 20), 4.0)),
            asof=date(2026, 11, 1),
            accessed_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
            settlement=False,
        )


def test_scorecard_refuses_to_mark_not_met_from_stale_data() -> None:
    with pytest.raises(
        ScorecardEvaluationError,
        match="cannot settle expired scorecard condition from stale data",
    ):
        _evaluate(
            _observations((date(2026, 7, 20), 4.0)),
            asof=date(2026, 11, 7),
            accessed_at=datetime(2026, 11, 7, 12, tzinfo=UTC),
            staleness_warn_days=7,
        )


def test_scorecard_separates_observation_deadline_from_vintage_cutoff(
    tmp_path: Path,
) -> None:
    indicators_db = tmp_path / "macro.sqlite"
    connection = initialize_database(indicators_db)
    try:
        definition = load_definitions().by_id()["jp.foreign_flows"]
        value = max(4.0, definition.plausible_min or 4.0)
        assert definition.plausible_max is None or value <= definition.plausible_max
        observation = ObservationRecord(
            series_id="jp.foreign_flows",
            observed_at=date(2026, 8, 21),
            value=value,
            unit=definition.unit,
            source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
            vintage_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        insert_observations(connection, [observation])
        connection.commit()

        evaluation = evaluate_scorecard(
            _foreign_flow_document(),
            reader=build_store_observation_reader(
                connection,
                series=load_definitions().series,
                vintage_cutoff=date(2026, 9, 1),
            ),
            provider_run_checker=(
                lambda _series_id, _provider, _start, _end, _completed_after, _asof: True
            ),
            providers={"jp.foreign_flows": "jquants_flows"},
            stale_after=lambda _series_id, observed_at: observed_at + timedelta(days=21),
            rules_revision="test-rules",
            asof=date(2026, 9, 1),
            accessed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            command="baibai-engine macro context scorecard --fixture",
            stores=ScorecardStores(
                context_db=str(Path("fixture-app.sqlite").resolve()),
                indicators_db=str(indicators_db.resolve()),
            ),
        )
    finally:
        connection.close()

    base = _result(evaluation, case="base", condition_index=1)
    assert base.status == "met"
    assert base.observation is not None
    assert (base.observation.observed_at, base.observation.vintage_at) == (
        date(2026, 8, 21),
        datetime(2026, 8, 27, tzinfo=UTC),
    )


def test_settlement_run_must_use_the_active_provider_after_the_watermark(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "macro.sqlite")
    try:
        record_provider_run(
            connection,
            provider="retired_provider",
            series_id="us.10y",
            start=date(2026, 7, 20),
            end=date(2026, 7, 31),
            started_at=datetime(2026, 8, 10, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
        assert not _has_provider_run(
            connection,
            series_id="us.10y",
            provider="fred_csv",
            start=date(2026, 7, 20),
            end=date(2026, 7, 31),
            completed_on_or_after=datetime(2026, 8, 7, tzinfo=UTC),
            asof=date(2026, 8, 15),
        )

        record_provider_run(
            connection,
            provider="fred_csv",
            series_id="us.10y",
            start=date(2026, 7, 20),
            end=date(2026, 7, 31),
            started_at=datetime(2026, 8, 10, tzinfo=UTC),
            status="ok",
            record_count=12,
        )
        connection.execute(
            "UPDATE provider_runs SET finished_at = ? WHERE provider = 'fred_csv'",
            (datetime(2026, 8, 16, tzinfo=UTC).isoformat(),),
        )
        assert not _has_provider_run(
            connection,
            series_id="us.10y",
            provider="fred_csv",
            start=date(2026, 7, 20),
            end=date(2026, 7, 31),
            completed_on_or_after=datetime(2026, 8, 7, tzinfo=UTC),
            asof=date(2026, 8, 15),
        )
        connection.execute(
            "UPDATE provider_runs SET finished_at = ? WHERE provider = 'fred_csv'",
            (datetime(2026, 8, 15, 14, 59, tzinfo=UTC).isoformat(),),
        )
        assert _has_provider_run(
            connection,
            series_id="us.10y",
            provider="fred_csv",
            start=date(2026, 7, 20),
            end=date(2026, 7, 31),
            completed_on_or_after=datetime(2026, 8, 7, tzinfo=UTC),
            asof=date(2026, 8, 15),
        )
    finally:
        connection.close()


def test_scorecard_cli_emits_citable_json_without_mutating_either_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_db = tmp_path / "app.sqlite"
    indicators_db = tmp_path / "macro.sqlite"
    document = _document()
    MacroContextService(context_db).publish(document, expected_head=None)
    connection = initialize_database(indicators_db)
    try:
        insert_observations(
            connection,
            [
                replace(
                    _observations((date(2026, 7, 20), 4.0))[0],
                    vintage_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
                )
            ],
        )
        record_provider_run(
            connection,
            provider="frb_h15",
            series_id="us.10y",
            start=date(2026, 7, 20),
            end=date(2026, 7, 20),
            started_at=datetime(2026, 7, 20, 11, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
        connection.execute(
            "UPDATE provider_runs SET finished_at = ? WHERE provider = 'frb_h15'",
            (datetime(2026, 7, 20, 11, 30, tzinfo=UTC).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    before = (_digest(context_db), _digest(indicators_db))
    transaction_states: list[bool] = []

    def checked_provider_run(
        connection: sqlite3.Connection,
        *,
        series_id: str,
        provider: str,
        start: date,
        end: date,
        completed_on_or_after: datetime | None,
        asof: date,
    ) -> bool:
        transaction_states.append(connection.in_transaction)
        return _has_provider_run(
            connection,
            series_id=series_id,
            provider=provider,
            start=start,
            end=end,
            completed_on_or_after=completed_on_or_after,
            asof=asof,
        )

    monkeypatch.setattr(
        "baibai_engine.macro.context.scorecard._has_provider_run",
        checked_provider_run,
    )

    exit_code = context_main(
        [
            "--db",
            str(context_db),
            "scorecard",
            "--context-id",
            document.context_id,
            "--asof",
            "2026-07-20",
            "--indicators-db",
            str(indicators_db),
            "--format",
            "json",
        ],
        now=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    assert exit_code == 0
    assert transaction_states
    assert all(transaction_states)
    assert before == (_digest(context_db), _digest(indicators_db))
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "macro-scorecard-evaluation"
    assert payload["stores"] == {
        "context_db": str(context_db.resolve()),
        "indicators_db": str(indicators_db.resolve()),
    }
    assert {item["status"] for item in payload["results"]} == {"met", "pending"}
    snapshot = payload["machine_snapshot"]
    current_rules_revision = rules_revision(DEFAULT_RULES_PATH)
    assert snapshot == {
        "kind": "macro-scorecard-evaluation",
        "input_id": scorecard_snapshot_input_id(
            context_id="macro-context-2026-07-19-base",
            snapshot_asof=date(2026, 7, 20),
            rules_revision=current_rules_revision,
            context_db=str(context_db.resolve()),
            indicators_db=str(indicators_db.resolve()),
            result_digest=snapshot["result_digest"],
        ),
        "context_id": "macro-context-2026-07-19-base",
        "rules_revision": current_rules_revision,
        "context_db": str(context_db.resolve()),
        "indicators_db": str(indicators_db.resolve()),
        "result_digest": snapshot["result_digest"],
        "command": (
            f"baibai-engine macro context --db {context_db.resolve()} scorecard "
            "--context-id macro-context-2026-07-19-base "
            f"--asof 2026-07-20 --indicators-db {indicators_db.resolve()} "
            f"--rules {DEFAULT_RULES_PATH.resolve()} "
            "--format json"
        ),
        "snapshot_asof": "2026-07-20",
        "observation_as_of": "2026-07-20",
        "accessed_at": "2026-07-20T12:00:00Z",
        "status": "ok",
        "used_for": ("macro-context-2026-07-19-base の scenario scorecard 採点"),
    }

    future_exit = context_main(
        [
            "--db",
            str(context_db),
            "scorecard",
            "--context-id",
            document.context_id,
            "--asof",
            "2026-07-21",
            "--indicators-db",
            str(indicators_db),
            "--format",
            "json",
        ],
        now=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    captured = capsys.readouterr()
    assert future_exit == 1
    assert "is in the future" in captured.err
    assert before == (_digest(context_db), _digest(indicators_db))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
