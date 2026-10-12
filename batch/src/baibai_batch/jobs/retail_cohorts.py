"""Fixed daily acquisition cohorts and private checkpoints; no network or storage I/O."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from baibai_batch.analysis.io import canonical_json
from baibai_engine.batch_api import (
    RetailAnalystRatingObservation,
    RetailCollectionOutcome,
    RetailModelValuationObservation,
    RetailObservation,
    RetailQuantsScoreObservation,
    RetailSource,
    retail_parse_failure_reasons,
)

MAX_COHORT_TICKERS = 150
DEFAULT_COHORT_TICKERS = 50
PLAN_FORMAT = "retail-daily-plan-v1"
JOURNAL_FORMAT = "retail-daily-journal-v1"
_TICKER = re.compile(r"[0-9][0-9A-Z]{3}")
type PairState = Literal["success", "normal_null", "failure", "unattempted", "uncertain"]
_STATES = frozenset({"success", "normal_null", "failure", "unattempted", "uncertain"})
_FAILURE_REASONS = retail_parse_failure_reasons | frozenset(
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
_UNCERTAIN_REASONS = frozenset(
    {"inflight", "wall_clock_limit", "request_budget", "uncertain_inflight"}
)


@dataclass(frozen=True)
class Cohort:
    index: int
    tickers: tuple[str, ...]
    pairs: tuple[tuple[RetailSource, str], ...]


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    tickers: tuple[str, ...]
    universe_binding: str
    collector_binding: str
    cohort_size: int
    sources: tuple[RetailSource, ...]

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.run_id, self.universe_binding, self.collector_binding)
        ):
            raise ValueError("plan requires fixed nonempty bindings")
        if type(self.cohort_size) is not int or not 1 <= self.cohort_size <= MAX_COHORT_TICKERS:
            raise ValueError("cohort size must be one to 150 tickers")
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
            or any(source not in {"monex", "rakuten", "kabumap"} for source in self.sources)
        ):
            raise ValueError("plan requires distinct supported sources")

    @property
    def cohorts(self) -> tuple[Cohort, ...]:
        return tuple(
            Cohort(
                index, chunk, tuple((source, ticker) for source in self.sources for ticker in chunk)
            )
            for index, start in enumerate(range(0, len(self.tickers), self.cohort_size))
            for chunk in (self.tickers[start : start + self.cohort_size],)
        )

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(plan_bytes(self)).hexdigest()


def make_plan(
    *,
    run_id: str,
    tickers: Sequence[str],
    universe_binding: str,
    collector_binding: str,
    cohort_size: int = DEFAULT_COHORT_TICKERS,
    sources: Sequence[RetailSource] = ("monex", "rakuten", "kabumap"),
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


def _has_value(observation: RetailObservation) -> bool:
    if isinstance(observation, RetailModelValuationObservation):
        return any(
            getattr(observation, metric) is not None
            for metric in ("consensus_target", "per_company_forecast", "per_consensus", "pbr")
        )
    if isinstance(observation, RetailQuantsScoreObservation):
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
    return observation.rating is not None


@dataclass(frozen=True)
class PairEntry:
    source: RetailSource
    ticker: str
    state: PairState
    reason: str | None
    http_status: int | None = None
    observation: RetailObservation | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(cast("object", self.source), str)
            or self.source not in {"monex", "rakuten", "kabumap"}
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
        if self.state in {"success", "normal_null"}:
            observation = self.observation
            expected = (
                RetailModelValuationObservation
                if self.source == "monex"
                else RetailQuantsScoreObservation
                if self.source == "kabumap"
                else RetailAnalystRatingObservation
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
            allowed = (
                _FAILURE_REASONS
                if self.state == "failure"
                else _UNATTEMPTED_REASONS
                if self.state == "unattempted"
                else _UNCERTAIN_REASONS
            )
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
class CohortJournal:
    plan_sha256: str
    cohort_index: int
    entries: tuple[PairEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            not isinstance(entry, PairEntry) for entry in self.entries
        ):
            raise ValueError("journal requires immutable typed pair entries")

    @property
    def observations(self) -> tuple[RetailObservation, ...]:
        return tuple(entry.observation for entry in self.entries if entry.observation is not None)

    @property
    def halt_reason(self) -> str | None:
        if any(entry.state == "uncertain" for entry in self.entries):
            return "inflight_uncertain"
        for reason in ("access_denied", "rate_limited"):
            if any(entry.reason == reason for entry in self.entries):
                return reason
        return None


def _validate_journal(plan: RunPlan, journal: CohortJournal) -> None:
    if journal.plan_sha256 != plan.plan_sha256:
        raise ValueError("journal binding differs from the fixed plan")
    if type(journal.cohort_index) is not int or not 0 <= journal.cohort_index < len(plan.cohorts):
        raise ValueError("journal has an unknown cohort")
    if (
        tuple((entry.source, entry.ticker) for entry in journal.entries)
        != plan.cohorts[journal.cohort_index].pairs
    ):
        raise ValueError("journal pairs are duplicated, missing, or differ from the fixed cohort")


def new_journal(plan: RunPlan, cohort_index: int) -> CohortJournal:
    if type(cohort_index) is not int or not 0 <= cohort_index < len(plan.cohorts):
        raise ValueError("unknown cohort")
    return CohortJournal(
        plan.plan_sha256,
        cohort_index,
        tuple(
            PairEntry(source, ticker, "unattempted", "not_started")
            for source, ticker in plan.cohorts[cohort_index].pairs
        ),
    )


def _replace_entry(journal: CohortJournal, entry: PairEntry) -> CohortJournal:
    if (entry.source, entry.ticker) not in {(item.source, item.ticker) for item in journal.entries}:
        raise ValueError("outcome is outside the fixed cohort")
    return replace(
        journal,
        entries=tuple(
            entry if (item.source, item.ticker) == (entry.source, entry.ticker) else item
            for item in journal.entries
        ),
    )


def mark_inflight(
    plan: RunPlan, journal: CohortJournal, source: RetailSource, ticker: str
) -> CohortJournal:
    _validate_journal(plan, journal)
    if journal.halt_reason is not None:
        raise ValueError("daily run cannot start another pair after a halt")
    current = next(
        (item for item in journal.entries if (item.source, item.ticker) == (source, ticker)), None
    )
    if current is None or current.state != "unattempted":
        raise ValueError("pair is outside the cohort or was already attempted")
    return _replace_entry(journal, PairEntry(source, ticker, "uncertain", "inflight"))


def begin_rate_limited_retry(
    plan: RunPlan, journal: CohortJournal, source: RetailSource, ticker: str
) -> CohortJournal:
    """Only the caller's validated durable retry reservation may reopen a known 429."""
    _validate_journal(plan, journal)
    current = next(
        (entry for entry in journal.entries if (entry.source, entry.ticker) == (source, ticker)),
        None,
    )
    if current is None or (current.state, current.reason, current.http_status) != (
        "failure",
        "rate_limited",
        429,
    ):
        raise ValueError("retry requires this pair's confirmed HTTP 429")
    if any(
        entry != current
        and (entry.state == "uncertain" or entry.reason in {"access_denied", "rate_limited"})
        for entry in journal.entries
    ):
        raise ValueError("another halted pair blocks this retry")
    return _replace_entry(journal, PairEntry(source, ticker, "uncertain", "inflight"))


def record_outcome(
    plan: RunPlan,
    journal: CohortJournal,
    outcome: RetailCollectionOutcome,
    observation: RetailObservation | None = None,
) -> CohortJournal:
    _validate_journal(plan, journal)
    if outcome.status == "observed":
        if observation is None:
            raise ValueError("observed outcome requires its typed observation")
        state: PairState = "success" if _has_value(observation) else "normal_null"
        reason = None if state == "success" else "all_values_missing"
    elif outcome.status == "not_attempted":
        state, reason = "unattempted", outcome.reason or "not_started"
    elif outcome.status == "incomplete":
        state, reason = "uncertain", outcome.reason or "uncertain_inflight"
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
        outcome.source, outcome.ticker, state, reason, outcome.http_status, observation
    )
    current = next(
        (
            item
            for item in journal.entries
            if (item.source, item.ticker) == (entry.source, entry.ticker)
        ),
        None,
    )
    if current is None:
        raise ValueError("outcome is outside the fixed cohort")
    if current == entry:
        return journal
    if current.state != "unattempted" and not (
        current.state == "uncertain" and current.reason == "inflight"
    ):
        raise ValueError("an existing pair payload cannot change")
    if state != "unattempted" and current.state != "uncertain":
        raise ValueError("an attempted outcome requires a saved inflight marker")
    if current.state == "uncertain" and state == "unattempted":
        raise ValueError("an inflight request cannot become unattempted")
    return _replace_entry(journal, entry)


def summarize(
    plan: RunPlan, journals: Sequence[CohortJournal], *, require_all: bool = False
) -> dict[str, object]:
    seen: set[int] = set()
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    halts: Counter[str] = Counter()
    for journal in journals:
        _validate_journal(plan, journal)
        if journal.cohort_index in seen:
            raise ValueError("duplicate cohort checkpoint")
        seen.add(journal.cohort_index)
        if journal.halt_reason is not None:
            halts[journal.halt_reason] += 1
        for entry in journal.entries:
            states[entry.state] += 1
            if entry.reason is not None:
                reasons[entry.reason] += 1
    missing = len(plan.cohorts) - len(seen)
    if require_all and missing:
        raise ValueError("missing cohort checkpoints")
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
        "halt_reasons": dict(sorted(halts.items())),
        "whole_universe_accounting": missing == 0,
        "attempted_completeness": missing == 0
        and states["unattempted"] == states["uncertain"] == 0,
        "success_completeness": states["success"] + states["normal_null"] == expected,
    }


def validate_resume(
    plan: RunPlan,
    journals: Sequence[CohortJournal],
    *,
    retryable_pairs: frozenset[tuple[RetailSource, str]] = frozenset(),
) -> None:
    summarize(plan, journals)
    if any(
        entry.state == "uncertain"
        or entry.reason == "access_denied"
        or (entry.reason == "rate_limited" and (entry.source, entry.ticker) not in retryable_pairs)
        for journal in journals
        for entry in journal.entries
    ):
        raise ValueError("daily run is halted by blocked access or an uncertain request")


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
        raise ValueError("unexpected checkpoint fields")
    return cast("dict[str, object]", value)


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON checkpoint field")
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
        tuple(cast("list[RetailSource]", data["sources"])),
    )
    if plan_bytes(plan) != canonical_json(data):
        raise ValueError("saved cohorts differ from their fixed plan")
    return plan


def journal_bytes(plan: RunPlan, journal: CohortJournal) -> bytes:
    _validate_journal(plan, journal)
    return canonical_json(
        {
            "format": JOURNAL_FORMAT,
            "plan_sha256": journal.plan_sha256,
            "cohort_index": journal.cohort_index,
            "entries": [
                {
                    "source": item.source,
                    "ticker": item.ticker,
                    "state": item.state,
                    "reason": item.reason,
                    "http_status": item.http_status,
                    "observation": item.observation.model_dump(mode="json")
                    if item.observation is not None
                    else None,
                }
                for item in journal.entries
            ],
        }
    )


def load_journal(plan: RunPlan, payload: bytes) -> CohortJournal:
    data = _object(
        json.loads(payload, object_pairs_hook=_unique_object),
        {"format", "plan_sha256", "cohort_index", "entries"},
    )
    if data["format"] != JOURNAL_FORMAT or not isinstance(data["entries"], list):
        raise ValueError("invalid daily journal format")
    entries = []
    for value in data["entries"]:
        entry = _object(
            value, {"source", "ticker", "state", "reason", "http_status", "observation"}
        )
        observation = None
        if entry["observation"] is not None:
            model = (
                RetailModelValuationObservation
                if entry["source"] == "monex"
                else RetailQuantsScoreObservation
                if entry["source"] == "kabumap"
                else RetailAnalystRatingObservation
            )
            observation = model.model_validate(entry["observation"])
        entries.append(
            PairEntry(
                cast("RetailSource", entry["source"]),
                cast("str", entry["ticker"]),
                cast("PairState", entry["state"]),
                cast("str | None", entry["reason"]),
                cast("int | None", entry["http_status"]),
                observation,
            )
        )
    journal = CohortJournal(
        cast("str", data["plan_sha256"]), cast("int", data["cohort_index"]), tuple(entries)
    )
    _validate_journal(plan, journal)
    return journal
