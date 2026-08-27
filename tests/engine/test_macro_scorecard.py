from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest
from tests.helpers.indicator_store import observations, store_reader
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
    _latest_vintages,
    _matches,
    evaluate_scorecard,
)
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
)
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH, rules_revision


def _document(*, deadline: str = "2026-10-31") -> MacroContextDocument:
    return MacroContextDocument.model_validate(macro_context_payload(scorecard_deadline=deadline))


_US10Y_SOURCE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"


def _observations(*points: tuple[date, float]) -> tuple[ObservationRecord, ...]:
    """Daily 10y readings, each vintaged at the start of the day it was observed."""

    return observations(
        "us.10y",
        points,
        source_url=_US10Y_SOURCE,
        vintage_at=lambda observed_at: datetime.combine(
            observed_at, datetime.min.time(), tzinfo=UTC
        ),
    )


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


def _evaluate(
    observations: Sequence[ObservationRecord],
    *,
    asof: date,
    accessed_at: datetime,
) -> ScorecardEvaluation:
    return evaluate_scorecard(
        _document(),
        reader=store_reader(observations),
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
    assert bear.status == "not_met"


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


def test_scorecard_settles_at_the_declared_deadline() -> None:
    evaluation = _evaluate(
        (),
        asof=date(2026, 10, 31),
        accessed_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    bear = _result(evaluation, case="bear", condition_index=1)
    assert bear.status == "not_met"
    assert bear.observation is None


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


def test_scorecard_rejects_a_future_asof() -> None:
    with pytest.raises(ScorecardEvaluationError, match="is in the future"):
        _evaluate(
            _observations((date(2026, 7, 20), 4.0)),
            asof=date(2026, 7, 21),
            accessed_at=datetime(2026, 7, 20, 14, 59, tzinfo=UTC),
        )


def test_scorecard_cli_emits_citable_json_without_mutating_either_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
        connection.commit()
    finally:
        connection.close()
    before = (_digest(context_db), _digest(indicators_db))
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
