"""Serial bounded acquisition of public pages, never publication."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime

import requests
from pydantic import ValidationError

from baibai_engine.data_collection.equity_web import http as source_http
from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse, Role, Source
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
    kabumap,
    minkabu,
    monex,
    rakuten,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    parse_source_html,
)
from baibai_engine.machine_analysis.observations.external_assessments.identity import (
    PAIR_ACQUISITION_POLICY_VERSION,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import Observation

DEFAULT_TICKERS = ("7203", "9432", "6758")
MAX_TICKERS = 3
MAX_REQUESTS = 9
DEFAULT_PAIR_TIME_LIMIT_SECONDS = 120
DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS = 30


PARSE_FAILURE_REASONS = frozenset(
    {
        "source_identity",
        "ticker_identity",
        "missing_theory_table",
        "changed_theory_table",
        "changed_theory_label",
        "changed_theory_cell",
        "incomplete_theory_table",
        "unexpected_numeric_cell",
        "missing_official_iframe",
        "changed_official_iframe_route",
        "changed_official_iframe_binding",
        "missing_ticker_identity",
        "no_estimates_identity_unconfirmed",
        "missing_rating_section",
        "changed_rating_table",
        "changed_rating_headers",
        "changed_rating_cell",
        "response_encoding",
        "missing_analyst_forecast",
        "unconfirmed_no_analyst_coverage",
        "changed_analyst_forecast",
        "conflicting_forecast_period",
        "missing_quants_date",
        "missing_quants_scores",
        "changed_quants_score",
    }
)


@dataclass(frozen=True)
class CollectionOutcome:
    source: Source
    ticker: str
    status: str
    reason: str | None = None
    http_status: int | None = None
    attempt_id: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    observations: tuple[Observation, ...]
    outcomes: tuple[CollectionOutcome, ...]
    requests: int
    stop_reason: str | None = None
    last_response_finished_at_utc: datetime | None = None
    # Fully received, decompressed response.content only; never wire byte counts.
    response_content_bytes: int | None = None
    # Confirmed 429 only. Raw Retry-After headers never leave the receiver.
    retry_after_not_before_utc: datetime | None = None
    retry_after_unrepresentable: bool = False

    def summary(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return {
            "observations": len(self.observations),
            "requests": self.requests,
            "outcome_counts": counts,
            "outcomes": [asdict(outcome) for outcome in self.outcomes],
            "stop_reason": self.stop_reason,
            "retry_after_not_before_utc": self.retry_after_not_before_utc.isoformat()
            if self.retry_after_not_before_utc is not None
            else None,
            "retry_after_unrepresentable": self.retry_after_unrepresentable,
        }


def collect(
    tickers: Sequence[str] = DEFAULT_TICKERS,
    *,
    sources: Sequence[Source] = ("monex", "rakuten"),
    request_budget: int = MAX_REQUESTS,
    time_limit_seconds: float = 60,
    request_timeout_seconds: float = 10,
    session: requests.Session | None = None,
    on_received_html: Callable[[EquityWebSourceResponse], None] | None = None,
) -> CollectionResult:
    """Acquire a predeclared small set; receipt times come only from live responses."""
    selected = tuple(normalize_ticker(ticker) for ticker in tickers)
    if not 1 <= len(selected) <= MAX_TICKERS or len(set(selected)) != len(selected):
        raise ValueError("collection requires one to three distinct explicit tickers")
    if (
        not sources
        or len(set(sources)) != len(sources)
        or any(source not in {"monex", "rakuten", "kabumap", "minkabu"} for source in sources)
    ):
        raise ValueError("collection requires distinct supported sources")
    if not 1 <= request_budget <= MAX_REQUESTS:
        raise ValueError("request budget must be one to nine")
    if not math.isfinite(time_limit_seconds) or not 0 < time_limit_seconds <= 120:
        raise ValueError("wall-clock limit must be positive and at most 120 seconds")
    if not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 30:
        raise ValueError("request timeout must be positive and at most 30 seconds")
    client = session if session is not None else requests.Session()
    observations: list[Observation] = []
    outcomes: list[CollectionOutcome] = []
    used = 0
    stop_reason = None
    retry_after_not_before_utc = None
    retry_after_unrepresentable = False
    active: tuple[Source, str] | None = None
    active_attempt_id: str | None = None
    started_requests = 0
    deadline = time.monotonic() + time_limit_seconds
    last_request_finished: float | None = None

    def receive(
        url: str, *, role: Role, parent_receipt_id: str | None = None
    ) -> tuple[str, datetime, str | None]:
        nonlocal used, last_request_finished
        if used >= request_budget:
            raise _RequestLimit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise source_http.TimeLimit
        if last_request_finished is not None and not (
            isinstance(client, source_http.PairSession) and client.rate_limiter is not None
        ):
            wait_until = last_request_finished + source_http.MIN_REQUEST_INTERVAL_SECONDS
            while (wait_seconds := wait_until - time.monotonic()) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise source_http.TimeLimit
                time.sleep(min(wait_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise source_http.TimeLimit
        used += 1
        try:
            assert active is not None
            assert active_attempt_id is not None
            return source_http.receive_html(
                client,
                url,
                min(request_timeout_seconds, remaining),
                attempt_id=active_attempt_id,
                source=active[0],
                ticker=active[1],
                role=role,
                parent_receipt_id=parent_receipt_id,
                on_received_html=on_received_html,
            )
        finally:
            last_request_finished = time.monotonic()

    try:
        with source_http.wall_clock_limit(time_limit_seconds):
            for source in sources:
                for index, ticker in enumerate(selected):
                    active = (source, ticker)
                    active_attempt_id = str(uuid.uuid4())
                    started_requests = used
                    try:
                        if source == "monex":
                            url = monex.public_url(ticker)
                            html, received, _ = receive(url, role="page")
                        elif source == "minkabu":
                            url = minkabu.public_url(ticker)
                            try:
                                html, received, _ = receive(url, role="page")
                            except source_http.HttpFailure as exc:
                                if exc.status != 404:
                                    raise
                                # 404 alone proves nothing. Only the identified issuer's
                                # explicit public "対象外" display can become normal-null.
                                url = minkabu.profile_url(ticker)
                                html, received, _ = receive(url, role="page")
                        elif source == "kabumap":
                            url = kabumap.public_url(ticker)
                            html, received, _ = receive(url, role="page")
                        else:
                            url = rakuten.public_url(ticker)
                            html, _, page_receipt_id = receive(url, role="page")
                            url = rakuten.iframe_url(html, ticker=ticker, source_url=url)
                            html, received, _ = receive(
                                url, role="iframe", parent_receipt_id=page_receipt_id
                            )
                        observation = parse_source_html(
                            html,
                            source=source,
                            ticker=ticker,
                            source_url=url,
                            fetched_at_utc=received,
                        )
                        observations.append(observation)
                        outcomes.append(
                            CollectionOutcome(
                                source,
                                ticker,
                                "observed",
                                http_status=200,
                                attempt_id=active_attempt_id,
                            )
                        )
                    except source_http.HttpFailure as exc:
                        if exc.status == 429:
                            retry_after_not_before_utc = exc.retry_after_not_before_utc
                            retry_after_unrepresentable = exc.retry_after_unrepresentable
                        category = (
                            "access_denied"
                            if exc.status == 403
                            else "rate_limited"
                            if exc.status == 429
                            else "http_failure"
                        )
                        outcomes.append(
                            CollectionOutcome(
                                source,
                                ticker,
                                category,
                                http_status=exc.status,
                                attempt_id=active_attempt_id,
                            )
                        )
                        if exc.status in {403, 429}:
                            outcomes.extend(
                                CollectionOutcome(source, pending, "not_attempted", category)
                                for pending in selected[index + 1 :]
                            )
                            break
                    except requests.Timeout:
                        outcomes.append(
                            CollectionOutcome(
                                source, ticker, "request_timeout", attempt_id=active_attempt_id
                            )
                        )
                    except requests.RequestException:
                        outcomes.append(
                            CollectionOutcome(
                                source, ticker, "transport_failure", attempt_id=active_attempt_id
                            )
                        )
                    except (ParseError, source_http.ResponseEncodingError) as exc:
                        code = exc.args[0] if len(exc.args) == 1 else None
                        reason = (
                            code
                            if isinstance(code, str) and code in PARSE_FAILURE_REASONS
                            else "unclassified_parse_failure"
                        )
                        outcomes.append(
                            CollectionOutcome(
                                source, ticker, "parse_failure", reason, 200, active_attempt_id
                            )
                        )
                    except ValidationError:
                        outcomes.append(
                            CollectionOutcome(
                                source,
                                ticker,
                                "parse_failure",
                                "observation_contract",
                                200,
                                active_attempt_id,
                            )
                        )
    except _RequestLimit:
        stop_reason = "request_budget"
    except source_http.TimeLimit:
        stop_reason = "wall_clock_limit"
    finally:
        if session is None:
            client.close()
    if stop_reason is not None:
        seen = {(outcome.source, outcome.ticker) for outcome in outcomes}
        for pending_source in sources:
            for pending_ticker in selected:
                if (pending_source, pending_ticker) not in seen:
                    attempted = (
                        pending_source,
                        pending_ticker,
                    ) == active and used > started_requests
                    outcomes.append(
                        CollectionOutcome(
                            pending_source,
                            pending_ticker,
                            "incomplete" if attempted else "not_attempted",
                            stop_reason,
                            attempt_id=active_attempt_id if attempted else None,
                        )
                    )
    return CollectionResult(
        tuple(observations),
        tuple(outcomes),
        used,
        stop_reason,
        retry_after_not_before_utc=retry_after_not_before_utc,
        retry_after_unrepresentable=retry_after_unrepresentable,
    )


class _RequestLimit(Exception):
    pass


def collector_binding(*, source: Source | None = None) -> str:
    """Bind only the selected source's parser and shared transport policy."""
    parsers = {
        "monex": monex.PARSER_VERSION,
        "rakuten": rakuten.PARSER_VERSION,
        "kabumap": kabumap.PARSER_VERSION,
        "minkabu": minkabu.PARSER_VERSION,
    }
    if source is not None and source not in parsers:
        raise ValueError("unsupported external assessment source")
    selected = tuple(parsers) if source is None else (source,)
    limits = {"monex": 1, "rakuten": 2, "kabumap": 1, "minkabu": 2}
    policy = {
        "format": PAIR_ACQUISITION_POLICY_VERSION,
        "parsers": {name: parsers[name] for name in selected},
        "request_headers": {
            name: source_http.MINKABU_PUBLIC_HEADERS if name == "minkabu" else {}
            for name in selected
        },
        "request_interval_seconds": source_http.MIN_REQUEST_INTERVAL_SECONDS,
        "pair_time_limit_seconds": DEFAULT_PAIR_TIME_LIMIT_SECONDS,
        "request_timeout_seconds": DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
        "source_request_limits": {name: limits[name] for name in selected},
        "source_start_interval_seconds": source_http.SOURCE_REQUEST_INTERVAL_SECONDS,
        "source_hosts": {name: source_http.SOURCE_HOSTS[name] for name in selected},
    }
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def collect_pair(
    ticker: str,
    *,
    source: Source,
    on_response: Callable[[datetime, int, int], None] | None = None,
    on_received_html: Callable[[EquityWebSourceResponse], None] | None = None,
    time_limit_seconds: float = DEFAULT_PAIR_TIME_LIMIT_SECONDS,
    request_timeout_seconds: float = DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
    rate_limiter: source_http.HostRateLimiter | None = None,
) -> CollectionResult:
    """Acquire one fixed pair attempt; retry and cohort commit belong to the caller.

    No HTTP request reserves durable state. The caller may repeat uncommitted
    GETs after a process interruption. Ordinary TLS/proxy, serial host limits,
    finite pair/HTTP deadlines and source identity checks remain unchanged.
    """
    ticker = normalize_ticker(ticker)
    if source not in {"monex", "rakuten", "kabumap", "minkabu"}:
        raise ValueError("pair collection requires a supported source")
    if not math.isfinite(time_limit_seconds) or not 0 < time_limit_seconds <= 120:
        raise ValueError("wall-clock limit must be positive and at most 120 seconds")
    if not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 30:
        raise ValueError("request timeout must be positive and at most 30 seconds")
    deadline = time.monotonic() + time_limit_seconds
    client = session if session is not None else requests.Session()
    wrapped: source_http.PairSession | None = None
    try:
        adapter = client.adapters.get("https://")
        if (
            not client.trust_env
            or client.verify is not True
            or not isinstance(adapter, requests.adapters.HTTPAdapter)
            or adapter.max_retries.total != 0
        ):
            raise ValueError("pair collection requires ordinary proxy/TLS and no retries")
        wrapped = source_http.PairSession(client, deadline, on_response, rate_limiter)
        remaining = min(time_limit_seconds, deadline - time.monotonic())
        if remaining <= 0:
            return CollectionResult(
                (),
                (CollectionOutcome(source, ticker, "not_attempted", "wall_clock_limit"),),
                0,
                "wall_clock_limit",
                response_content_bytes=0,
            )
        result = collect(
            [ticker],
            sources=(source,),
            request_budget=2 if source in {"rakuten", "minkabu"} else 1,
            time_limit_seconds=remaining,
            request_timeout_seconds=request_timeout_seconds,
            session=wrapped,
            on_received_html=on_received_html,
        )
        result = replace(
            result,
            requests=wrapped.requests,
            last_response_finished_at_utc=wrapped.finished_at_utc,
            response_content_bytes=wrapped.content_bytes,
        )
        if wrapped.requests == 0:
            return replace(
                result,
                outcomes=(CollectionOutcome(source, ticker, "not_attempted", result.stop_reason),),
            )
        if any(outcome.status == "incomplete" for outcome in result.outcomes):
            return replace(
                result,
                outcomes=(
                    CollectionOutcome(
                        source,
                        ticker,
                        "request_timeout",
                        attempt_id=result.outcomes[0].attempt_id,
                    ),
                ),
                stop_reason="wall_clock_limit",
            )
        if result.outcomes[0].http_status in {403, 429}:
            result = replace(result, stop_reason=result.outcomes[0].status)
        return result
    finally:
        if wrapped is not None:
            wrapped.close()
        if session is None:
            client.close()
