from __future__ import annotations

import math
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import assert_never
from zoneinfo import ZoneInfo

from ..reading.rules import load_reading_rules
from . import db
from .db import IndicatorsSchemaError, ObservationRecord
from .definitions import IndicatorDefinitions, SeriesDefinition, load_definitions
from .providers import (
    FetchContext,
    IndicatorsProviderError,
    RangeReplacementPolicy,
    fetch_observations,
    provider_spec,
)
from .providers.base import StoreReader
from .providers.formulas import FORMULAS

DEFAULT_LATEST_LOOKBACK_DAYS = 370
LATEST_FETCH_LOOKBACK_DAYS = {
    "daily": 14,
    "weekly": 60,
    "monthly": DEFAULT_LATEST_LOOKBACK_DAYS,
}
PROVIDER_FETCH_ATTEMPTS = 2
PROVIDER_FETCH_RETRY_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class QueryResult:
    series: SeriesDefinition
    observations: tuple[ObservationRecord, ...]
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class RefreshSuccess:
    series_id: str
    result: QueryResult


@dataclass(frozen=True, slots=True)
class RefreshFailure:
    series_id: str
    message: str


# One series' outcome in a multi-series refresh, so a caller reports every
# failure instead of only the one that stopped the pass.
type RefreshOutcome = RefreshSuccess | RefreshFailure


class IndicatorsService:
    def __init__(self, db_path: Path = db.DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def list_series(self, *, category: str | None = None) -> tuple[SeriesDefinition, ...]:
        definitions = load_definitions()
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            return tuple(
                sorted(
                    (
                        series
                        for series in definitions.series
                        if category is None or series.category == category
                    ),
                    key=lambda series: (series.priority, series.series_id),
                )
            )
        finally:
            conn.close()

    def search(self, query: str) -> tuple[SeriesDefinition, ...]:
        definitions = load_definitions()
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            return tuple(
                sorted(
                    definitions.search(query),
                    key=lambda series: (series.priority, series.series_id),
                )
            )
        finally:
            conn.close()

    def get_range(
        self,
        series_id: str,
        *,
        start: date,
        end: date,
        refresh: bool = False,
    ) -> QueryResult:
        if end < start:
            raise ValueError("--end must be on or after --start")
        definitions = load_definitions()
        if series_id not in definitions.by_id():
            raise KeyError(f"unknown indicator series: {series_id}")
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            with FetchContext(
                store_reader=_store_reader(conn),
                purpose="refresh" if refresh else "read",
            ) as context:
                return self._get_range(
                    conn, series_id, start=start, end=end, refresh=refresh, context=context
                )
        finally:
            conn.close()

    def retract(
        self,
        series_id: str,
        targets: Sequence[tuple[date, datetime]],
    ) -> tuple[db.RetractionOutcome, ...]:
        """Withdraw the latest vintage of observation dates without removing any history."""

        definitions = load_definitions()
        if series_id not in definitions.by_id():
            raise KeyError(f"unknown indicator series: {series_id}")
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            _require_no_stale_derived_dependents(conn, series_id, targets)
            retractions = db.retract_observations(
                conn,
                series_id,
                targets,
                vintage_at=datetime.now(UTC),
            )
            conn.commit()
            return retractions
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def refresh_all_history(self, series_id: str, *, end: date) -> QueryResult:
        definitions = load_definitions()
        if series_id not in definitions.by_id():
            raise KeyError(f"unknown indicator series: {series_id}")
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            _prune_registry_for_refresh(conn, definitions)
            with FetchContext(store_reader=_store_reader(conn), purpose="rebuild") as context:
                return self._refresh_all_history(conn, series_id, end=end, context=context)
        finally:
            conn.close()

    def refresh_series(
        self,
        series_ids: Sequence[str],
        *,
        start: date | None,
        end: date,
    ) -> list[RefreshOutcome]:
        """Refresh several series in one pass, isolating per-series failures.

        ``start`` is ``None`` for an all-history refresh (each provider's declared
        floor). One store connection and one fetch context serve the whole pass, so
        a bulk source file is downloaded once for every series that maps to it and a
        browser-backed provider pays at most one launch. A failure that belongs to
        one series is captured and the pass continues, so one broken source cannot
        leave the rest of the registry stale; a store-level failure (schema, IO)
        still aborts the pass because it invalidates every remaining series.
        """

        if start is not None and end < start:
            raise ValueError("--end must be on or after --start")
        definitions = load_definitions()
        registered_ids = definitions.by_id()
        if not any(series_id in registered_ids for series_id in series_ids):
            return [
                RefreshFailure(series_id, f"unknown indicator series: {series_id}")
                for series_id in series_ids
            ]
        outcomes: list[RefreshOutcome] = []
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            _prune_registry_for_refresh(conn, definitions)
            with FetchContext(
                store_reader=_store_reader(conn),
                purpose="rebuild" if start is None else "refresh",
            ) as context:
                for series_id in series_ids:
                    try:
                        result = (
                            self._refresh_all_history(conn, series_id, end=end, context=context)
                            if start is None
                            else self._get_range(
                                conn,
                                series_id,
                                start=start,
                                end=end,
                                refresh=True,
                                context=context,
                            )
                        )
                    except (sqlite3.Error, IndicatorsSchemaError):
                        raise
                    except Exception as exc:
                        # Deliberately broad: whatever one series' provider raises —
                        # including a bug in its parser or an unwrapped third-party
                        # error — belongs to that series alone. Narrowing this would
                        # let one unforeseen error type strand every later series.
                        outcomes.append(RefreshFailure(series_id, _failure_message(exc)))
                        continue
                    outcomes.append(RefreshSuccess(series_id, result))
        finally:
            conn.close()
        return outcomes

    def _get_range(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        *,
        start: date,
        end: date,
        refresh: bool,
        context: FetchContext,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        spec = provider_spec(series.provider)
        point_in_time = spec.point_in_time_vintage
        cache_hit = (not refresh) and db.has_ok_coverage(conn, series_id, start, end)
        if not cache_hit:
            observations = self._fetch_and_store(
                conn, series, start=start, end=end, context=context
            )
        else:
            observations = list(
                db.observations_in_range(conn, series_id, start, end, point_in_time=point_in_time)
            )
        # Providers return observations in source order (ECB FX is newest-first);
        # normalize to ascending observed_at so callers get a stable chronology
        # regardless of cache-hit vs provider-fetch path.
        ordered = sorted(observations, key=lambda item: item.observed_at)
        return QueryResult(series, tuple(ordered), cache_hit=cache_hit)

    def _refresh_all_history(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        *,
        end: date,
        context: FetchContext,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        spec = provider_spec(series.provider)
        if spec.all_history_rolling_years is not None:
            start = _years_before(_today_jst(), spec.all_history_rolling_years)
            if end < start:
                raise IndicatorsProviderError(
                    f"all-history end {end.isoformat()} precedes the {spec.name} "
                    f"rolling history floor {start.isoformat()}"
                )
        elif spec.all_history_start is not None:
            start = spec.all_history_start
        else:
            raise IndicatorsProviderError(
                f"all-history start is not configured for provider {series.provider}"
            )
        observations = self._fetch_and_store(
            conn,
            series,
            start=start,
            end=end,
            context=context,
            trim_before_first=spec.trim_before_first,
            remove_other_sources=True,
            range_replacement=spec.range_replacement_for(series),
            require_observations=True,
        )
        ordered = sorted(observations, key=lambda item: item.observed_at)
        return QueryResult(series, tuple(ordered), cache_hit=False)

    def get_latest(self, series_id: str, *, refresh: bool = False) -> QueryResult:
        end = _today_jst()
        definitions = load_definitions()
        registered = definitions.by_id()
        if series_id not in registered:
            raise KeyError(f"unknown indicator series: {series_id}")
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            series = db.get_series(conn, series_id)
            spec = provider_spec(series.provider)
            cached_latest = db.latest_observation(
                conn, series_id, on_or_before=end, point_in_time=spec.point_in_time_vintage
            )
            if (
                not refresh
                and cached_latest is not None
                and _latest_cache_is_fresh(series, cached_latest, asof=end)
            ):
                return QueryResult(series, (cached_latest,), cache_hit=True)
        finally:
            conn.close()
        lookback_days = LATEST_FETCH_LOOKBACK_DAYS.get(
            series.frequency, DEFAULT_LATEST_LOOKBACK_DAYS
        )
        start = end - timedelta(days=lookback_days)
        result = self.get_range(
            series_id,
            start=start,
            end=end,
            refresh=refresh or cached_latest is not None,
        )
        if not result.observations:
            return result
        return QueryResult(
            result.series,
            (max(result.observations, key=lambda item: item.observed_at),),
            cache_hit=result.cache_hit,
        )

    def _fetch_and_store(
        self,
        conn: sqlite3.Connection,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        context: FetchContext,
        trim_before_first: bool = False,
        remove_other_sources: bool = False,
        range_replacement: RangeReplacementPolicy = "none",
        require_observations: bool = False,
    ) -> list[ObservationRecord]:
        started_at = datetime.now(UTC)
        try:
            observations = _fetch_observations_with_retry(
                series, start=start, end=end, context=context
            )
            if require_observations and not observations:
                raise IndicatorsProviderError(
                    f"all-history refresh returned no observations for {series.series_id}"
                )
            _reject_observation_identity(series, observations)
            _reject_non_finite(series, observations)
            _reject_outside_plausible_range(series, observations)
            if range_replacement == "all_vintages":
                _require_complete_replacement(
                    conn,
                    series,
                    start=start,
                    end=end,
                    observations=observations,
                )
            # Every store-rewrite policy below spares what the store learned after the
            # provider's newest statement. A rewrite replaces what the provider is
            # restating; a retraction and the belief it put back are decisions about
            # rows the provider is not going to re-deliver, so dropping them would let
            # the merge restore the withdrawn row and undo the decision on the next
            # push. For a provider that stamps acquisition time this spares nothing
            # extra — its newest statement is now. A source that does re-deliver the
            # date buries the decision under a newer vintage, which is correct.
            kept_after = _newest_provider_statement(observations)
            if trim_before_first and observations:
                # FRED's current licensed delivery window defines reproducible
                # all-history coverage for a series.
                first_observed_at = min(item.observed_at for item in observations)
                conn.execute(
                    "DELETE FROM observations WHERE series_id = ? AND observed_at < ? "
                    "AND (? IS NULL OR vintage_at <= ?)",
                    (series.series_id, first_observed_at.isoformat(), kept_after, kept_after),
                )
            if remove_other_sources and observations:
                conn.execute(
                    "DELETE FROM observations WHERE series_id = ? AND source_url != ? "
                    "AND observed_at BETWEEN ? AND ? AND substr(vintage_at, 1, 10) <= ? "
                    "AND (? IS NULL OR vintage_at <= ?)",
                    (
                        series.series_id,
                        series.source_url,
                        start.isoformat(),
                        end.isoformat(),
                        end.isoformat(),
                        kept_after,
                        kept_after,
                    ),
                )
            if range_replacement != "none" and observations:
                first_observed_at = min(item.observed_at for item in observations)
                replacement_start = min(start, first_observed_at).isoformat()
                if range_replacement == "through_end_vintage":
                    conn.execute(
                        "DELETE FROM observations WHERE series_id = ? "
                        "AND observed_at BETWEEN ? AND ? "
                        "AND substr(vintage_at, 1, 10) <= ? "
                        "AND (? IS NULL OR vintage_at <= ?)",
                        (
                            series.series_id,
                            replacement_start,
                            end.isoformat(),
                            end.isoformat(),
                            kept_after,
                            kept_after,
                        ),
                    )
                elif range_replacement == "all_vintages":
                    conn.execute(
                        "DELETE FROM observations WHERE series_id = ? "
                        "AND observed_at BETWEEN ? AND ? "
                        "AND (? IS NULL OR vintage_at <= ?)",
                        (
                            series.series_id,
                            replacement_start,
                            end.isoformat(),
                            kept_after,
                            kept_after,
                        ),
                    )
                else:  # pragma: no cover - exhaustiveness guard over the policy literal
                    assert_never(range_replacement)
            db.insert_observations(conn, observations)
            db.delete_unchanged_vintages(conn, series.series_id)
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=_provider_run_coverage_end(
                    series, requested_end=end, observations=observations
                ),
                started_at=started_at,
                status="ok",
                record_count=len(observations),
            )
            conn.commit()
            return observations
        except Exception as exc:
            conn.rollback()
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=end,
                started_at=started_at,
                status="failed",
                record_count=0,
                error_message=str(exc),
            )
            conn.commit()
            raise


def _store_reader(conn: sqlite3.Connection) -> StoreReader:
    """Read a stored series through the live connection.

    Derived (local) providers read their input series with this, so they see
    inputs committed earlier in the same pass. spglobal_pmi reads it to skip
    months it already holds.
    """

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return db.observations_in_range(conn, series_id, start, end)

    return read


def _newest_provider_statement(observations: list[ObservationRecord]) -> str | None:
    """The newest vintage the provider just stated, or None when it stamps none itself."""

    vintages = [item.vintage_at for item in observations if item.vintage_at is not None]
    return max(vintages).isoformat() if vintages else None


def _require_no_stale_derived_dependents(
    conn: sqlite3.Connection,
    series_id: str,
    targets: Sequence[tuple[date, datetime]],
) -> None:
    """Refuse a retraction that would leave a derived value computed from it behind.

    A derived series stores its own observations, so withdrawing an input does not
    withdraw what was computed from it. Recomputation cannot repair it either: the
    withdrawn date stops appearing among the inputs, so the formula produces nothing for
    it and the stale row keeps winning on vintage — permanently, and silently.

    The dependency graph is small and a retraction is rare, so this refuses and names the
    exact follow-up rather than propagating on its own. Refusing before anything is
    written also keeps the store out of a half-repaired state.
    """

    dependents = tuple(
        sorted(
            derived_id for derived_id, formula in FORMULAS.items() if series_id in formula.inputs
        )
    )
    if not dependents:
        return
    stale: list[str] = []
    for derived_id in dependents:
        for observed_at, _ in targets:
            vintage_at = db.latest_vintage_at(conn, derived_id, observed_at)
            if vintage_at is None:
                continue
            stale.append(
                f"macro retract {derived_id} --observed-at {observed_at.isoformat()} "
                f"--expected-vintage {vintage_at.isoformat()}"
            )
    if stale:
        raise IndicatorsProviderError(
            f"retracting {series_id} would leave derived observations computed from it; "
            "withdraw these first:\n" + "\n".join(stale)
        )


def _prune_registry_for_refresh(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> None:
    """Commit explicit registry pruning and report every destructive change."""

    transaction_id = uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_generation = db.registry_generation(conn)
        if stored_generation > definitions.generation:
            raise ValueError(
                "indicator registry is newer than this client "
                f"(store={stored_generation}, client={definitions.generation}); "
                "refusing refresh"
            )
        unregistered = db.unregistered_series_ids(conn, definitions)
        if unregistered and stored_generation == definitions.generation:
            # One generation is one membership: changing the canonical series set
            # requires a new digest and a higher generation. Series the registry
            # does not name while the generations match therefore means another
            # working tree wrote its own membership at this generation, and its
            # facts are not this client's to delete.
            raise ValueError(
                "indicator store holds series this registry does not name at the same "
                f"generation (generation={stored_generation}, "
                f"unregistered={', '.join(unregistered)}); refusing refresh"
            )
        pruned = db.prune_definitions(conn, definitions)
        if pruned:
            pending = "\n".join(
                f"registry-prune-pending\t{result.series_id}\t"
                f"observations={result.observation_rows}\t"
                f"provider_runs={result.provider_run_rows}\t"
                f"transaction={transaction_id}"
                for result in pruned
            )
            # A durable stdout stream is the prerequisite for commit. Pending
            # records make a later commit/output failure distinguishable from a
            # completed prune without adding an audit-history subsystem.
            print(pending, flush=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if pruned:
        committed = "\n".join(
            f"registry-prune\t{result.series_id}\t"
            f"observations={result.observation_rows}\t"
            f"provider_runs={result.provider_run_rows}\t"
            f"transaction={transaction_id}"
            for result in pruned
        )
        print(committed, flush=True)


def _latest_cache_is_fresh(
    series: SeriesDefinition,
    observation: ObservationRecord,
    *,
    asof: date,
) -> bool:
    """Use the same publication-lag-aware observation age as the L2 reading."""

    rule = load_reading_rules().resolve(
        series_id=series.series_id,
        frequency=series.frequency,
    )
    return not rule.is_stale(observation.observed_at, asof=asof)


def _reject_non_finite(series: SeriesDefinition, observations: list[ObservationRecord]) -> None:
    """Keep NaN / ±inf out of the store.

    ``float("nan")`` and ``float("1e999")`` parse from source text as ordinary
    numbers, and once stored they silently poison every derived computation,
    percentile and JSON export that reads the series. This is the one gate every
    provider passes through, so no provider can introduce one on its own.
    """

    for observation in observations:
        if not math.isfinite(observation.value):
            raise IndicatorsProviderError(
                f"{series.series_id} {observation.observed_at.isoformat()}: "
                f"non-finite value {observation.value}"
            )


def _reject_observation_identity(
    series: SeriesDefinition,
    observations: list[ObservationRecord],
) -> None:
    """Bind every provider row to the requested series and its declared unit."""

    for observation in observations:
        if observation.series_id != series.series_id:
            raise IndicatorsProviderError(
                f"{series.series_id}: provider returned observation for {observation.series_id}"
            )
        if observation.unit != series.unit:
            raise IndicatorsProviderError(
                f"{series.series_id} {observation.observed_at.isoformat()}: "
                f"provider returned unit {observation.unit!r}; expected {series.unit!r}"
            )


def _reject_outside_plausible_range(
    series: SeriesDefinition,
    observations: list[ObservationRecord],
) -> None:
    """Reject finite values that indicate a likely column, scale, or unit mismatch."""

    low = series.plausible_min
    high = series.plausible_max
    for observation in observations:
        if (low is not None and observation.value < low) or (
            high is not None and observation.value > high
        ):
            rendered_low = "-inf" if low is None else f"{low:g}"
            rendered_high = "inf" if high is None else f"{high:g}"
            raise IndicatorsProviderError(
                f"{series.series_id} {observation.observed_at.isoformat()}: "
                f"value {observation.value:g} outside plausible range "
                f"[{rendered_low}, {rendered_high}]"
            )


def _failure_message(exc: Exception) -> str:
    # KeyError stringifies with quotes around the message; unwrap it so an unknown
    # series reads the same as any other failure.
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _today_jst() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def _provider_run_coverage_end(
    series: SeriesDefinition,
    *,
    requested_end: date,
    observations: list[ObservationRecord],
) -> date:
    if series.frequency == "daily" and observations:
        return min(requested_end, max(item.observed_at for item in observations))
    return requested_end


def _require_complete_replacement(
    conn: sqlite3.Connection,
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    observations: list[ObservationRecord],
) -> None:
    """Reject a destructive rebuild that drops a previously represented period.

    A formula may intentionally change the date inside a declared month or quarter,
    so replacement coverage is compared at the registry cadence rather than by exact
    ``observed_at``. Missing periods indicate that the candidate was built from a
    partial input store; keeping the existing rows is safer than committing a shorter
    history as a successful all-history rebuild.
    """

    existing = db.observations_in_range(conn, series.series_id, start, end)
    if not existing:
        return
    candidate_periods = {
        _declared_period(item.observed_at, frequency=series.frequency) for item in observations
    }
    missing = sorted(
        {_declared_period(item.observed_at, frequency=series.frequency) for item in existing}
        - candidate_periods
    )
    if missing:
        raise IndicatorsProviderError(
            f"{series.series_id} rebuild would drop {len(missing)} existing "
            f"{series.frequency} period(s), starting at {missing[0]}; "
            "refresh complete inputs before replacing derived history"
        )


def _declared_period(observed_at: date, *, frequency: str) -> str:
    if frequency == "monthly":
        return observed_at.strftime("%Y-%m")
    if frequency == "quarterly":
        return f"{observed_at.year}-Q{(observed_at.month - 1) // 3 + 1}"
    if frequency == "weekly":
        iso_year, iso_week, _ = observed_at.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return observed_at.isoformat()


def _fetch_observations_with_retry(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext | None,
) -> list[ObservationRecord]:
    for attempt in range(1, PROVIDER_FETCH_ATTEMPTS + 1):
        try:
            return fetch_observations(series, start=start, end=end, context=context)
        except IndicatorsProviderError:
            # The response that produced this failure may be cached (a block page
            # arrives as HTTP 200), so drop the cache before retrying. Otherwise the
            # retry re-reads the same bytes and every later series sharing the URL
            # inherits the failure.
            if context is not None:
                context.discard_cached_bytes()
            if attempt == PROVIDER_FETCH_ATTEMPTS:
                raise
            time.sleep(PROVIDER_FETCH_RETRY_BACKOFF_SECONDS)
    raise AssertionError("unreachable provider retry loop")
