"""Fixed daily acquisition cohorts and confirmed cohort results; no network or storage I/O."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Literal, cast

from baibai_batch.storage.canonical_json import canonical_json
from baibai_batch.storage.external_assessment_identity import PLAN_FORMAT
from baibai_engine.batch_api import (
    ExternalAssessmentAnalystForecastObservation,
    ExternalAssessmentAnalystRatingObservation,
    ExternalAssessmentCollectionOutcome,
    ExternalAssessmentModelValuationObservation,
    ExternalAssessmentObservation,
    ExternalAssessmentQuantsScoreObservation,
    ExternalAssessmentSource,
    external_assessment_parse_failure_reasons,
)

MAX_COHORT_TICKERS = 50
DEFAULT_COHORT_TICKERS = 50
_TICKER = re.compile(r"[0-9][0-9A-Z]{3}")
type PairState = Literal["success", "normal_null", "failure", "unattempted"]
_STATES = frozenset({"success", "normal_null", "failure", "unattempted"})
_FAILURE_REASONS = external_assessment_parse_failure_reasons | frozenset(
    {
        "access_denied",
        "rate_limited",
        "http_failure",
        "request_timeout",
        "transport_failure",
        "unclassified_parse_failure",
        "observation_contract",
    }
)
_UNATTEMPTED_REASONS = frozenset(
    {
        "not_started",
        "access_denied",
        "rate_limited",
        "wall_clock_limit",
        "request_budget",
        "run_halted",
    }
)


@dataclass(frozen=True)
class Cohort:
    index: int
    tickers: tuple[str, ...]
    pairs: tuple[tuple[ExternalAssessmentSource, str], ...]


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    tickers: tuple[str, ...]
    universe_binding: str
    collector_binding: str
    cohort_size: int
    sources: tuple[ExternalAssessmentSource, ...]

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.run_id, self.universe_binding, self.collector_binding)
        ):
            raise ValueError("plan requires fixed nonempty bindings")
        if type(self.cohort_size) is not int or not 1 <= self.cohort_size <= MAX_COHORT_TICKERS:
            raise ValueError("cohort size must be one to 50 tickers")
        if (
            type(self.tickers) is not tuple
            or not self.tickers
            or any(type(t) is not str or _TICKER.fullmatch(t) is None for t in self.tickers)
            or self.tickers != tuple(sorted(set(self.tickers)))
        ):
            raise ValueError("plan requires sorted distinct canonical tickers")
        if (
            type(self.sources) is not tuple
            or not self.sources
            or any(type(source) is not str for source in self.sources)
            or len(set(self.sources)) != len(self.sources)
            or any(
                source not in {"monex", "rakuten", "kabumap", "minkabu"} for source in self.sources
            )
        ):
            raise ValueError("plan requires distinct supported sources")

    @cached_property
    def cohorts(self) -> tuple[Cohort, ...]:
        return tuple(
            Cohort(
                index, chunk, tuple((source, ticker) for source in self.sources for ticker in chunk)
            )
            for index, start in enumerate(range(0, len(self.tickers), self.cohort_size))
            for chunk in (self.tickers[start : start + self.cohort_size],)
        )

    @cached_property
    def plan_sha256(self) -> str:
        return hashlib.sha256(plan_bytes(self)).hexdigest()


def make_plan(
    *,
    run_id: str,
    tickers: Sequence[str],
    universe_binding: str,
    collector_binding: str,
    cohort_size: int = DEFAULT_COHORT_TICKERS,
    sources: Sequence[ExternalAssessmentSource] = ("monex", "rakuten", "kabumap", "minkabu"),
) -> RunPlan:
    if any(type(ticker) is not str or _TICKER.fullmatch(ticker) is None for ticker in tickers):
        raise ValueError("plan requires canonical ASCII tickers")
    if len(set(tickers)) != len(tickers):
        raise ValueError("duplicate universe ticker")
    return RunPlan(
        run_id,
        tuple(sorted(tickers)),
        universe_binding,
        collector_binding,
        cohort_size,
        tuple(sources),
    )


def _has_value(observation: ExternalAssessmentObservation) -> bool:
    if isinstance(observation, ExternalAssessmentModelValuationObservation):
        return any(
            getattr(observation, metric) is not None
            for metric in ("consensus_target", "per_company_forecast", "per_consensus", "pbr")
        )
    if isinstance(observation, ExternalAssessmentQuantsScoreObservation):
        return any(
            getattr(observation, name) is not None
            for name in (
                "growth_score",
                "undervaluation_score",
                "size_score",
                "technical_score",
                "financial_health_score",
                "market_trend_score",
                "composite_score",
                "industry_average_score",
            )
        )
    if isinstance(observation, ExternalAssessmentAnalystForecastObservation):
        return observation.rating_label is not None or observation.target_price_yen is not None
    return observation.rating is not None


@dataclass(frozen=True)
class PairEntry:
    source: ExternalAssessmentSource
    ticker: str
    state: PairState
    reason: str | None
    http_status: int | None = None
    observation: ExternalAssessmentObservation | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(cast("object", self.source), str)
            or self.source not in {"monex", "rakuten", "kabumap", "minkabu"}
            or not isinstance(cast("object", self.state), str)
            or self.state not in _STATES
        ):
            raise ValueError("unsupported pair source or state")
        if (
            type(self.ticker) is not str
            or _TICKER.fullmatch(self.ticker) is None
            or (self.reason is not None and type(self.reason) is not str)
        ):
            raise ValueError("invalid canonical pair identity or outcome reason")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid HTTP status")
        if self.state == "unattempted":
            if self.attempt_id is not None:
                raise ValueError("unattempted pair cannot reference an attempt")
        else:
            try:
                identity = uuid.UUID(self.attempt_id)
            except (TypeError, ValueError, AttributeError):
                raise ValueError("attempted pair requires a canonical attempt UUID") from None
            if str(identity) != self.attempt_id or identity.int == 0:
                raise ValueError("attempted pair requires a canonical attempt UUID")
        if self.state in {"success", "normal_null"}:
            observation = self.observation
            expected = (
                ExternalAssessmentModelValuationObservation
                if self.source == "monex"
                else ExternalAssessmentQuantsScoreObservation
                if self.source == "kabumap"
                else ExternalAssessmentAnalystForecastObservation
                if self.source == "minkabu"
                else ExternalAssessmentAnalystRatingObservation
            )
            if (
                not isinstance(observation, expected)
                or observation.ticker != self.ticker
                or observation.distributor != self.source
            ):
                raise ValueError("observation does not match its pair")
            if _has_value(observation) != (self.state == "success"):
                raise ValueError("pair state does not match displayed values")
            if self.http_status != 200 or self.reason != (
                None if self.state == "success" else "all_values_missing"
            ):
                raise ValueError("invalid observed outcome metadata")
        else:
            if self.observation is not None:
                raise ValueError("unconfirmed outcome cannot hold an observation")
            if self.state == "unattempted" and self.http_status is not None:
                raise ValueError("unattempted pair cannot have an HTTP response status")
            allowed = _FAILURE_REASONS if self.state == "failure" else _UNATTEMPTED_REASONS
            if self.reason not in allowed:
                raise ValueError("unsupported fixed outcome reason")
            if (
                self.reason == "access_denied"
                and self.state == "failure"
                and self.http_status != 403
            ):
                raise ValueError("access_denied requires HTTP 403")
            if (
                self.reason == "rate_limited"
                and self.state == "failure"
                and self.http_status != 429
            ):
                raise ValueError("rate_limited requires HTTP 429")
            if self.http_status in {403, 429} and (
                self.state != "failure"
                or self.reason != {403: "access_denied", 429: "rate_limited"}[self.http_status]
            ):
                raise ValueError("blocked HTTP outcome must halt the daily run")


@dataclass(frozen=True)
class CohortResult:
    plan_sha256: str
    cohort_index: int
    entries: tuple[PairEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            not isinstance(entry, PairEntry) for entry in self.entries
        ):
            raise ValueError("result requires immutable typed pair entries")

    @property
    def observations(self) -> tuple[ExternalAssessmentObservation, ...]:
        return tuple(entry.observation for entry in self.entries if entry.observation is not None)

    @property
    def complete(self) -> bool:
        return all(entry.state != "unattempted" for entry in self.entries)


def validate_result(plan: RunPlan, result: CohortResult) -> None:
    if result.plan_sha256 != plan.plan_sha256:
        raise ValueError("result binding differs from the fixed plan")
    if type(result.cohort_index) is not int or not 0 <= result.cohort_index < len(plan.cohorts):
        raise ValueError("result has an unknown cohort")
    if (
        tuple((entry.source, entry.ticker) for entry in result.entries)
        != plan.cohorts[result.cohort_index].pairs
    ):
        raise ValueError("result pairs are duplicated, missing, or differ from the fixed cohort")


def new_result(plan: RunPlan, cohort_index: int) -> CohortResult:
    if type(cohort_index) is not int or not 0 <= cohort_index < len(plan.cohorts):
        raise ValueError("unknown cohort")
    return CohortResult(
        plan.plan_sha256,
        cohort_index,
        tuple(
            PairEntry(source, ticker, "unattempted", "not_started")
            for source, ticker in plan.cohorts[cohort_index].pairs
        ),
    )


def record_outcome(
    plan: RunPlan,
    result: CohortResult,
    outcome: ExternalAssessmentCollectionOutcome,
    observation: ExternalAssessmentObservation | None = None,
) -> CohortResult:
    validate_result(plan, result)
    if outcome.status == "observed":
        if observation is None:
            raise ValueError("observed outcome requires its typed observation")
        state: PairState = "success" if _has_value(observation) else "normal_null"
        reason = None if state == "success" else "all_values_missing"
    elif outcome.status == "not_attempted":
        state, reason = "unattempted", outcome.reason or "not_started"
    else:
        if outcome.status not in {
            "access_denied",
            "rate_limited",
            "http_failure",
            "request_timeout",
            "transport_failure",
            "parse_failure",
        }:
            raise ValueError("unsupported collection outcome status")
        state, reason = (
            "failure",
            outcome.reason if outcome.status == "parse_failure" else outcome.status,
        )
    entry = PairEntry(
        outcome.source,
        outcome.ticker,
        state,
        reason,
        outcome.http_status,
        observation,
        outcome.attempt_id,
    )
    current = next(
        (
            item
            for item in result.entries
            if (item.source, item.ticker) == (entry.source, entry.ticker)
        ),
        None,
    )
    if current is None:
        raise ValueError("outcome is outside the fixed cohort")
    if current == entry:
        return result
    if current.state != "unattempted":
        raise ValueError("an existing pair payload cannot change")
    return replace(
        result,
        entries=tuple(
            entry if (item.source, item.ticker) == (entry.source, entry.ticker) else item
            for item in result.entries
        ),
    )


def summarize(
    plan: RunPlan, results: Sequence[CohortResult], *, require_all: bool = False
) -> dict[str, object]:
    seen: set[int] = set()
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for result in results:
        validate_result(plan, result)
        if result.cohort_index in seen:
            raise ValueError("duplicate cohort result")
        seen.add(result.cohort_index)
        for entry in result.entries:
            states[entry.state] += 1
            if entry.reason is not None:
                reasons[entry.reason] += 1
    missing = len(plan.cohorts) - len(seen)
    if require_all and missing:
        raise ValueError("missing cohort results")
    expected = len(plan.tickers) * len(plan.sources)
    recorded = states.total()
    states["unattempted"] += expected - recorded
    return {
        "expected_pairs": expected,
        "recorded_pairs": recorded,
        "unrecorded_pairs": expected - recorded,
        "cohorts": len(plan.cohorts),
        "recorded_cohorts": len(seen),
        "missing_cohorts": missing,
        "state_counts": {state: states[state] for state in sorted(_STATES)},
        "reason_counts": dict(sorted(reasons.items())),
        "whole_universe_accounting": missing == 0,
        "attempted_completeness": missing == 0 and states["unattempted"] == 0,
        "success_completeness": states["success"] + states["normal_null"] == expected,
    }


def plan_bytes(plan: RunPlan) -> bytes:
    return canonical_json(
        {
            "format": PLAN_FORMAT,
            "run_id": plan.run_id,
            "tickers": plan.tickers,
            "universe_binding": plan.universe_binding,
            "collector_binding": plan.collector_binding,
            "cohort_size": plan.cohort_size,
            "sources": plan.sources,
            "cohorts": [
                {"index": item.index, "tickers": item.tickers, "pairs": item.pairs}
                for item in plan.cohorts
            ],
        }
    )


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected fields")
    return cast("dict[str, object]", value)


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def load_plan(payload: bytes) -> RunPlan:
    data = _object(
        json.loads(payload, object_pairs_hook=_unique_object),
        {
            "format",
            "run_id",
            "tickers",
            "universe_binding",
            "collector_binding",
            "cohort_size",
            "sources",
            "cohorts",
        },
    )
    if (
        data["format"] != PLAN_FORMAT
        or not isinstance(data["tickers"], list)
        or not isinstance(data["sources"], list)
    ):
        raise ValueError("invalid daily plan format")
    plan = RunPlan(
        cast("str", data["run_id"]),
        tuple(cast("list[str]", data["tickers"])),
        cast("str", data["universe_binding"]),
        cast("str", data["collector_binding"]),
        cast("int", data["cohort_size"]),
        tuple(cast("list[ExternalAssessmentSource]", data["sources"])),
    )
    if plan_bytes(plan) != canonical_json(data):
        raise ValueError("saved cohorts differ from their fixed plan")
    return plan


def result_value(plan: RunPlan, result: CohortResult) -> dict[str, object]:
    validate_result(plan, result)
    return {
        "plan_sha256": result.plan_sha256,
        "cohort_index": result.cohort_index,
        "entries": [
            {
                "source": item.source,
                "ticker": item.ticker,
                "state": item.state,
                "reason": item.reason,
                "http_status": item.http_status,
                "attempt_id": item.attempt_id,
                "observation": item.observation.model_dump(mode="json")
                if item.observation is not None
                else None,
            }
            for item in result.entries
        ],
    }


def load_result(plan: RunPlan, value: object) -> CohortResult:
    data = _object(
        value,
        {"plan_sha256", "cohort_index", "entries"},
    )
    if not isinstance(data["entries"], list):
        raise ValueError("invalid cohort result format")
    entries = []
    for value in data["entries"]:
        entry = _object(
            value,
            {"source", "ticker", "state", "reason", "http_status", "observation", "attempt_id"},
        )
        observation = None
        if entry["observation"] is not None:
            model = (
                ExternalAssessmentModelValuationObservation
                if entry["source"] == "monex"
                else ExternalAssessmentQuantsScoreObservation
                if entry["source"] == "kabumap"
                else ExternalAssessmentAnalystForecastObservation
                if entry["source"] == "minkabu"
                else ExternalAssessmentAnalystRatingObservation
            )
            observation = model.model_validate(entry["observation"])
        entries.append(
            PairEntry(
                cast("ExternalAssessmentSource", entry["source"]),
                cast("str", entry["ticker"]),
                cast("PairState", entry["state"]),
                cast("str | None", entry["reason"]),
                cast("int | None", entry["http_status"]),
                observation,
                cast("str | None", entry["attempt_id"]),
            )
        )
    result = CohortResult(
        cast("str", data["plan_sha256"]), cast("int", data["cohort_index"]), tuple(entries)
    )
    validate_result(plan, result)
    return result
