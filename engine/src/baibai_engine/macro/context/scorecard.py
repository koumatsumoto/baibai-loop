"""Settle published macro scenario scorecards against the read-only L1 store."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.time import JST
from baibai_engine.macro.indicators import db as indicators_db
from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.reader import ObservationReader, build_store_observation_reader
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH, rules_revision

from .models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
    ScorecardCondition,
    ScorecardSnapshotInput,
    scorecard_snapshot_input_id,
)

type ScorecardStatus = Literal["met", "not_met", "pending"]
type ScenarioCase = Literal["base", "bear", "bull"]

_VINTAGE_POLICY = (
    "latest eligible vintage per observation; publication-quality vintages "
    "are clamped to snapshot_asof independently from observation deadlines"
)


class _OutputModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScorecardObservation(_OutputModel):
    observed_at: date
    value: float = Field(allow_inf_nan=False)
    unit: str
    vintage_at: datetime | None
    source_url: str


class ScorecardStores(_OutputModel):
    context_db: str
    indicators_db: str


class ScorecardResult(_OutputModel):
    case: ScenarioCase
    condition_index: int = Field(ge=1)
    series_id: str
    comparison: Literal["below", "at_or_below", "above", "at_or_above"]
    threshold: float = Field(allow_inf_nan=False)
    deadline: date
    evaluated_through: date
    status: ScorecardStatus
    observation: ScorecardObservation | None


class ScorecardEvaluation(_OutputModel):
    schema_version: Literal[1] = 1
    kind: Literal["macro-scorecard-evaluation"] = "macro-scorecard-evaluation"
    context_id: str
    context_as_of: date
    snapshot_asof: date
    rules_revision: str
    vintage_policy: str = _VINTAGE_POLICY
    stores: ScorecardStores
    results: tuple[ScorecardResult, ...]
    machine_snapshot: ScorecardSnapshotInput

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ScorecardEvaluationError(ValueError):
    pass


def evaluate_scorecard(
    document: MacroContextDocument,
    *,
    reader: ObservationReader,
    rules_revision: str,
    asof: date,
    accessed_at: datetime,
    command: str,
    stores: ScorecardStores,
) -> ScorecardEvaluation:
    """Evaluate every scenario condition without writing a scorecard ledger."""

    if asof < document.as_of:
        raise ScorecardEvaluationError(
            f"scorecard asof {asof} predates context as_of {document.as_of}"
        )
    if accessed_at.tzinfo is None or accessed_at.utcoffset() is None:
        raise ScorecardEvaluationError("scorecard accessed_at must include a timezone")
    access_date = accessed_at.astimezone(JST).date()
    if asof > access_date:
        raise ScorecardEvaluationError(
            f"scorecard asof {asof} is in the future at access date {access_date}"
        )
    start = document.as_of + timedelta(days=1)
    results: list[ScorecardResult] = []
    for scenario in document.scenarios:
        for index, condition in enumerate(scenario.scorecard, start=1):
            cutoff = min(asof, condition.deadline)
            observations = (
                _latest_vintages(reader(condition.series_id, start, cutoff))
                if start <= cutoff
                else ()
            )
            results.append(
                _evaluate_condition(
                    case=scenario.case,
                    condition_index=index,
                    condition=condition,
                    observations=observations,
                    asof=asof,
                    cutoff=cutoff,
                )
            )

    observation_dates = [
        result.observation.observed_at for result in results if result.observation is not None
    ]
    observation_as_of = max(observation_dates, default=None)
    result_payload = [result.model_dump(mode="json") for result in results]
    result_digest = hashlib.sha256(
        json.dumps(
            result_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    input_id = scorecard_snapshot_input_id(
        context_id=document.context_id,
        snapshot_asof=asof,
        rules_revision=rules_revision,
        context_db=stores.context_db,
        indicators_db=stores.indicators_db,
        result_digest=result_digest,
    )
    return ScorecardEvaluation(
        context_id=document.context_id,
        context_as_of=document.as_of,
        snapshot_asof=asof,
        rules_revision=rules_revision,
        stores=stores,
        results=tuple(results),
        machine_snapshot=ScorecardSnapshotInput(
            kind="macro-scorecard-evaluation",
            input_id=input_id,
            context_id=document.context_id,
            rules_revision=rules_revision,
            context_db=stores.context_db,
            indicators_db=stores.indicators_db,
            result_digest=result_digest,
            command=command,
            snapshot_asof=asof,
            observation_as_of=observation_as_of,
            accessed_at=accessed_at,
            status="ok",
            used_for=f"{document.context_id} の scenario scorecard 採点",
        ),
    )


def evaluate_scorecard_from_stores(
    *,
    context_db: Path | None,
    indicators_db_path: Path,
    context_id: str,
    asof: date,
    accessed_at: datetime,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> ScorecardEvaluation:
    document = load_context_document(context_db, context_id=context_id)
    definitions = load_definitions()
    revision = rules_revision(rules_path)
    resolved_context_db = database_path(context_db).resolve()
    resolved_indicators_db = indicators_db_path.resolve()
    stores = ScorecardStores(
        context_db=str(resolved_context_db),
        indicators_db=str(resolved_indicators_db),
    )
    command = shlex.join(
        (
            "baibai-engine",
            "macro",
            "context",
            "--db",
            str(resolved_context_db),
            "scorecard",
            "--context-id",
            context_id,
            "--asof",
            asof.isoformat(),
            "--indicators-db",
            str(resolved_indicators_db),
            "--rules",
            str(rules_path.resolve()),
            "--format",
            "json",
        )
    )
    connection = indicators_db.open_read_only_connection(indicators_db_path)
    try:
        # One read transaction keeps every condition on the same store snapshot.
        connection.execute("BEGIN")
        return evaluate_scorecard(
            document,
            reader=build_store_observation_reader(
                connection,
                series=definitions.series,
                vintage_cutoff=asof,
            ),
            rules_revision=revision,
            asof=asof,
            accessed_at=accessed_at,
            command=command,
            stores=stores,
        )
    finally:
        connection.close()


def load_context_document(
    path: Path | None,
    *,
    context_id: str,
) -> MacroContextDocument:
    """Load one current-contract context without initializing or migrating its store."""

    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT payload FROM macro_context WHERE context_id = ? AND schema_version = ?",
            (context_id, MACRO_CONTEXT_SCHEMA_VERSION),
        ).fetchone()
    if row is None:
        raise ScorecardEvaluationError(f"unknown context_id: {context_id}")
    return MacroContextDocument.model_validate_json(str(row[0]))


def _evaluate_condition(
    *,
    case: ScenarioCase,
    condition_index: int,
    condition: ScorecardCondition,
    observations: Sequence[ObservationRecord],
    asof: date,
    cutoff: date,
) -> ScorecardResult:
    first_met = next(
        (
            observation
            for observation in observations
            if _matches(
                observation.value,
                comparison=condition.comparison,
                threshold=condition.threshold,
            )
        ),
        None,
    )
    used: ObservationRecord | None
    if first_met is not None:
        status: ScorecardStatus = "met"
        used = first_met
    elif asof >= condition.deadline:
        status = "not_met"
        used = observations[-1] if observations else None
    else:
        status = "pending"
        used = observations[-1] if observations else None

    return ScorecardResult(
        case=case,
        condition_index=condition_index,
        series_id=condition.series_id,
        comparison=condition.comparison,
        threshold=condition.threshold,
        deadline=condition.deadline,
        evaluated_through=cutoff,
        status=status,
        observation=(
            None
            if used is None
            else ScorecardObservation(
                observed_at=used.observed_at,
                value=used.value,
                unit=used.unit,
                vintage_at=used.vintage_at,
                source_url=used.source_url,
            )
        ),
    )


def _latest_vintages(
    observations: Sequence[ObservationRecord],
) -> tuple[ObservationRecord, ...]:
    """Return one deterministic latest eligible vintage per observed date."""

    grouped: dict[date, list[ObservationRecord]] = {}
    for observation in observations:
        grouped.setdefault(observation.observed_at, []).append(observation)
    selected: list[ObservationRecord] = []
    missing_vintage = datetime.min.replace(tzinfo=UTC)
    for observed_at in sorted(grouped):
        candidates = grouped[observed_at]
        latest_vintage = max(candidate.vintage_at or missing_vintage for candidate in candidates)
        latest = [
            candidate
            for candidate in candidates
            if (candidate.vintage_at or missing_vintage) == latest_vintage
        ]
        if any(candidate != latest[0] for candidate in latest[1:]):
            raise ScorecardEvaluationError(
                "conflicting scorecard observations share an identity: "
                f"observed_at={observed_at} vintage_at={latest_vintage.isoformat()}"
            )
        selected.append(latest[0])
    return tuple(selected)


def _matches(
    value: float,
    *,
    comparison: Literal["below", "at_or_below", "above", "at_or_above"],
    threshold: float,
) -> bool:
    match comparison:
        case "below":
            return value < threshold
        case "at_or_below":
            return value <= threshold
        case "above":
            return value > threshold
        case "at_or_above":
            return value >= threshold
    assert_never(comparison)


__all__ = [
    "ScorecardEvaluation",
    "ScorecardEvaluationError",
    "ScorecardObservation",
    "ScorecardResult",
    "ScorecardStores",
    "evaluate_scorecard",
    "evaluate_scorecard_from_stores",
    "load_context_document",
]
