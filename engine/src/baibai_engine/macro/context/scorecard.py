"""Settle published macro scenario scorecards against the read-only L1 store."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
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
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
    rules_revision,
)

from .models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
    ScorecardCondition,
    ScorecardSnapshotInput,
    scorecard_snapshot_input_id,
)

type ScorecardStatus = Literal["met", "not_met", "pending"]
type ScenarioCase = Literal["base", "bear", "bull"]
type ProviderRunChecker = Callable[[str, str, date, date, datetime | None, date], bool]
type StaleAfterResolver = Callable[[str, date], date]

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
    settlement_ready_on: date
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
    provider_run_checker: ProviderRunChecker,
    providers: Mapping[str, str],
    stale_after: StaleAfterResolver,
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
    # A report stays readable after one of its series is retired, but it cannot be
    # settled: there is no provider to prove a match against. Say which series, so the
    # answer is "this scenario is unsettleable" rather than an unexplained lookup failure.
    retired = sorted(
        {
            condition.series_id
            for scenario in document.scenarios
            for condition in scenario.scorecard
            if condition.series_id not in providers
        }
    )
    if retired:
        raise ScorecardEvaluationError(
            "cannot settle a scorecard on series the registry no longer defines: "
            + ", ".join(retired)
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
                    start=start,
                    provider_run_checker=provider_run_checker,
                    provider=providers[condition.series_id],
                    stale_after=stale_after,
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
    rules = load_reading_rules(rules_path)
    revision = rules_revision(rules_path)
    resolved_rules = {
        definition.series_id: rules.resolve(
            series_id=definition.series_id,
            frequency=definition.frequency,
        )
        for definition in definitions.series
    }
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
        # Observation and provider-run reads jointly define one citable machine
        # snapshot. An explicit read transaction prevents a concurrent refresh from
        # moving one side of that evidence boundary between SELECT statements.
        connection.execute("BEGIN")
        return evaluate_scorecard(
            document,
            reader=build_store_observation_reader(
                connection,
                series=definitions.series,
                vintage_cutoff=asof,
            ),
            provider_run_checker=(
                lambda series_id, provider, start, end, completed_after, score_asof: (
                    _has_provider_run(
                        connection,
                        series_id=series_id,
                        provider=provider,
                        start=start,
                        end=end,
                        completed_on_or_after=completed_after,
                        asof=score_asof,
                    )
                )
            ),
            providers={
                definition.series_id: definition.provider for definition in definitions.series
            },
            stale_after=lambda series_id, observed_at: resolved_rules[series_id].stale_after(
                observed_at
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
    start: date,
    provider_run_checker: ProviderRunChecker,
    provider: str,
    stale_after: StaleAfterResolver,
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
    settlement_ready_on = stale_after(condition.series_id, condition.deadline)
    if first_met is not None:
        if first_met.vintage_at is None:
            raise ScorecardEvaluationError(
                "cannot prove first scorecard match without vintage_at: "
                f"{case}[{condition_index}] {condition.series_id} "
                f"observed_at={first_met.observed_at}"
            )
        _require_provider_run(
            provider_run_checker,
            case=case,
            condition_index=condition_index,
            series_id=condition.series_id,
            provider=provider,
            start=start,
            end=first_met.observed_at,
            completed_on_or_after=first_met.vintage_at,
            asof=asof,
        )
        status: ScorecardStatus = "met"
        used = first_met
    elif asof >= settlement_ready_on:
        status = "not_met"
        if not observations:
            raise ScorecardEvaluationError(
                "cannot settle expired scorecard condition without an observation: "
                f"{case}[{condition_index}] {condition.series_id} "
                f"through {condition.deadline}"
            )
        used = observations[-1]
        _require_provider_run(
            provider_run_checker,
            case=case,
            condition_index=condition_index,
            series_id=condition.series_id,
            provider=provider,
            start=start,
            end=condition.deadline,
            completed_on_or_after=datetime.combine(
                settlement_ready_on,
                time.min,
                tzinfo=JST,
            ),
            asof=asof,
        )
        observation_stale_after = stale_after(condition.series_id, used.observed_at)
        if condition.deadline > observation_stale_after:
            raise ScorecardEvaluationError(
                "cannot settle expired scorecard condition from stale data: "
                f"{case}[{condition_index}] {condition.series_id} "
                f"last_observed_at={used.observed_at} deadline={condition.deadline} "
                f"stale_after={observation_stale_after}"
            )
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
        settlement_ready_on=settlement_ready_on,
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


def _require_provider_run(
    checker: ProviderRunChecker,
    *,
    case: ScenarioCase,
    condition_index: int,
    series_id: str,
    provider: str,
    start: date,
    end: date,
    completed_on_or_after: datetime | None,
    asof: date,
) -> None:
    if checker(series_id, provider, start, end, completed_on_or_after, asof):
        return
    raise ScorecardEvaluationError(
        "cannot settle scorecard condition without an eligible successful "
        f"provider run: {case}[{condition_index}] {series_id} {start}/{end} "
        f"provider={provider} completed_on_or_after={completed_on_or_after} asof={asof}"
    )


def _has_provider_run(
    connection: sqlite3.Connection,
    *,
    series_id: str,
    provider: str,
    start: date,
    end: date,
    completed_on_or_after: datetime | None,
    asof: date,
) -> bool:
    """Whether the active provider completed a full-window refresh in the time bounds."""

    rows = connection.execute(
        "SELECT finished_at FROM provider_runs "
        "WHERE series_id = ? AND provider = ? AND status = 'ok' "
        "AND record_count > 0 AND range_start <= ? AND range_end >= ? "
        "ORDER BY finished_at DESC",
        (
            series_id,
            provider,
            start.isoformat(),
            end.isoformat(),
        ),
    ).fetchall()
    upper = datetime.combine(asof + timedelta(days=1), time.min, tzinfo=JST)
    for row in rows:
        finished_at = datetime.fromisoformat(str(row[0]))
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            continue
        if finished_at >= upper:
            continue
        if completed_on_or_after is not None and finished_at < completed_on_or_after:
            continue
        return True
    return False


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
