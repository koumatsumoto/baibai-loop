"""Serial bounded acquisition into private normalized staging, never publication."""

from __future__ import annotations

import hashlib
import json
import math
import re
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Literal

import requests
from pydantic import ValidationError

from baibai_engine.market.ticker import normalize_ticker

from .models import AnalystRatingObservation, ModelValuationObservation, Observation
from .sources import ParseError, monex, rakuten

DEFAULT_TICKERS = ("7203", "9432", "6758")
MAX_TICKERS = 3
MAX_REQUESTS = 9
MIN_REQUEST_INTERVAL_SECONDS = 2.0
DEFAULT_PAIR_TIME_LIMIT_SECONDS = 120
DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS = 30
# Increment this policy version whenever pair acquisition semantics change.
PAIR_ACQUISITION_POLICY_VERSION = "retail-pair-acquisition-v1"
STAGING_FORMAT = "retail-dynamics-staging-v1"
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
    }
)
type Source = Literal["monex", "rakuten"]


@dataclass(frozen=True)
class CollectionOutcome:
    source: Source
    ticker: str
    status: str
    reason: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class CollectionResult:
    observations: tuple[Observation, ...]
    outcomes: tuple[CollectionOutcome, ...]
    requests: int
    stop_reason: str | None = None
    last_response_finished_at_utc: datetime | None = None
    # Fully received, decompressed response.content only; never wire byte counts.
    response_content_bytes: int | None = None

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
        }


class _TimeLimit(Exception):
    pass


@contextmanager
def _wall_clock_limit(seconds: float) -> Iterator[None]:
    # requests' read timeout alone permits a indefinitely trickling response.
    # Linux CLI execution has an interruptible main thread and a real deadline.
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("bounded collection requires the main thread")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("bounded collection requires an unused wall-clock timer")

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise _TimeLimit

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _HttpFailure(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _receive(session: requests.Session, url: str, timeout: float) -> tuple[str, datetime]:
    # Redirects would add unbudgeted requests and can redirect to login pages.
    with session.get(url, timeout=timeout, allow_redirects=False) as response:
        if response.status_code != 200:
            raise _HttpFailure(response.status_code)
        body = response.content
        received = datetime.now(UTC)
        content_type = response.headers.get("Content-Type", "")
        charset = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.I)
        meta = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", body[:4096], re.I)
        encoding = charset[1] if charset else meta[1].decode("ascii") if meta else "utf-8"
        try:
            return body.decode(encoding), received
        except (LookupError, UnicodeDecodeError):
            raise ParseError("response_encoding") from None


def collect(
    tickers: Sequence[str] = DEFAULT_TICKERS,
    *,
    sources: Sequence[Source] = ("monex", "rakuten"),
    request_budget: int = MAX_REQUESTS,
    time_limit_seconds: float = 60,
    request_timeout_seconds: float = 10,
    session: requests.Session | None = None,
) -> CollectionResult:
    """Acquire a predeclared small set; receipt times come only from live responses."""
    selected = tuple(normalize_ticker(ticker) for ticker in tickers)
    if not 1 <= len(selected) <= MAX_TICKERS or len(set(selected)) != len(selected):
        raise ValueError("collection requires one to three distinct explicit tickers")
    if (
        not sources
        or len(set(sources)) != len(sources)
        or any(source not in {"monex", "rakuten"} for source in sources)
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
    active: tuple[Source, str] | None = None
    started_requests = 0
    deadline = time.monotonic() + time_limit_seconds
    last_request_finished: float | None = None

    def receive(url: str) -> tuple[str, datetime]:
        nonlocal used, last_request_finished
        if used >= request_budget:
            raise _RequestLimit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _TimeLimit
        if last_request_finished is not None:
            wait_until = last_request_finished + MIN_REQUEST_INTERVAL_SECONDS
            while (wait_seconds := wait_until - time.monotonic()) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _TimeLimit
                time.sleep(min(wait_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _TimeLimit
        used += 1
        try:
            return _receive(client, url, min(request_timeout_seconds, remaining))
        finally:
            last_request_finished = time.monotonic()

    try:
        with _wall_clock_limit(time_limit_seconds):
            for source in sources:
                for index, ticker in enumerate(selected):
                    active = (source, ticker)
                    started_requests = used
                    try:
                        if source == "monex":
                            url = monex.public_url(ticker)
                            html, received = receive(url)
                            observation: Observation = monex.parse_html(
                                html, ticker=ticker, source_url=url, fetched_at_utc=received
                            )
                        else:
                            url = rakuten.public_url(ticker)
                            html, _ = receive(url)
                            url = rakuten.iframe_url(html, ticker=ticker, source_url=url)
                            html, received = receive(url)
                            observation = rakuten.parse_html(
                                html, ticker=ticker, source_url=url, fetched_at_utc=received
                            )
                        observations.append(observation)
                        outcomes.append(
                            CollectionOutcome(source, ticker, "observed", http_status=200)
                        )
                    except _HttpFailure as exc:
                        category = (
                            "access_denied"
                            if exc.status == 403
                            else "rate_limited"
                            if exc.status == 429
                            else "http_failure"
                        )
                        outcomes.append(
                            CollectionOutcome(source, ticker, category, http_status=exc.status)
                        )
                        if exc.status in {403, 429}:
                            outcomes.extend(
                                CollectionOutcome(source, pending, "not_attempted", category)
                                for pending in selected[index + 1 :]
                            )
                            break
                    except requests.Timeout:
                        outcomes.append(CollectionOutcome(source, ticker, "request_timeout"))
                    except requests.RequestException:
                        outcomes.append(CollectionOutcome(source, ticker, "transport_failure"))
                    except ParseError as exc:
                        code = exc.args[0] if len(exc.args) == 1 else None
                        reason = (
                            code
                            if isinstance(code, str) and code in PARSE_FAILURE_REASONS
                            else "unclassified_parse_failure"
                        )
                        outcomes.append(
                            CollectionOutcome(source, ticker, "parse_failure", reason, 200)
                        )
                    except ValidationError:
                        outcomes.append(
                            CollectionOutcome(
                                source, ticker, "parse_failure", "observation_contract", 200
                            )
                        )
    except _RequestLimit:
        stop_reason = "request_budget"
    except _TimeLimit:
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
                        )
                    )
    return CollectionResult(tuple(observations), tuple(outcomes), used, stop_reason)


class _RequestLimit(Exception):
    pass


def collector_binding() -> str:
    """Bind a resumable plan to acquisition policy and the actual parser versions."""
    policy = {
        "format": PAIR_ACQUISITION_POLICY_VERSION,
        "monex_parser": monex.PARSER_VERSION,
        "rakuten_parser": rakuten.PARSER_VERSION,
        "request_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
        "initial_pair_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
        "pair_time_limit_seconds": DEFAULT_PAIR_TIME_LIMIT_SECONDS,
        "request_timeout_seconds": DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
        "source_request_limits": {"monex": 1, "rakuten": 2},
    }
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _checkpoint_callback(callback: Callable[[], None]) -> None:
    try:
        callback()
    except _TimeLimit:
        raise
    except Exception:
        # Checkpoint failures must not become broker transport/parser outcomes,
        # and exception messages can contain signed URLs or private payloads.
        raise RuntimeError("pair_checkpoint_callback_failed") from None


class _PairSession(requests.Session):
    def __init__(
        self,
        client: requests.Session,
        deadline: float,
        before_request: Callable[[], None],
        on_response: Callable[[datetime, int, int], None] | None,
    ) -> None:
        super().__init__()
        self.client = client
        self.deadline = deadline
        self.before_request = before_request
        self.on_response = on_response
        self.initial_wait_until = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS
        self.requests = 0
        self.reservation_started = False
        self.finished_at_utc: datetime | None = None
        self.content_bytes = 0

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _TimeLimit
        return remaining

    def get(self, url: str | bytes, params: Any = None, **kwargs: Any) -> requests.Response:
        if kwargs.get("allow_redirects") is not False:
            raise ValueError("pair redirects are forbidden")
        if self.requests == 0:
            # A fresh two-second wait also covers immediate process/cohort
            # resumes without inferring elapsed time from a persisted clock.
            while (wait := self.initial_wait_until - time.monotonic()) > 0:
                time.sleep(min(wait, self._remaining()))
        self._remaining()
        self.reservation_started = True
        _checkpoint_callback(self.before_request)
        remaining = self._remaining()
        kwargs["timeout"] = min(float(kwargs["timeout"]), remaining)
        if params is not None:
            kwargs["params"] = params
        self.requests += 1
        response = self.client.get(url, **kwargs)
        try:
            body = response.content
            finished = datetime.now(UTC)
            self.finished_at_utc = finished
            self.content_bytes += len(body)
            callback = self.on_response
            if callback is not None:
                _checkpoint_callback(lambda: callback(finished, self.requests, len(body)))
            return response
        except BaseException:
            response.close()
            raise


def collect_pair(
    ticker: str,
    *,
    source: Source,
    before_request: Callable[[], None],
    on_outcome: Callable[[CollectionResult], None],
    on_response: Callable[[datetime, int, int], None] | None = None,
    time_limit_seconds: float = DEFAULT_PAIR_TIME_LIMIT_SECONDS,
    request_timeout_seconds: float = DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> CollectionResult:
    """Acquire one fixed pair once, with durable callbacks inside one wall-clock budget.

    Each transport invocation follows before_request. Only determinate outcomes
    invoke on_outcome; uncertain_inflight requires retaining the durable marker
    and stopping the whole run. A zero-request timeout before any reservation
    remains unattempted; an interrupted checkpoint reservation is uncertain.
    403/429 stop_reason halts the caller's whole run. The bounded collect API
    retains its existing source-only stop behavior.
    """
    ticker = normalize_ticker(ticker)
    if source not in {"monex", "rakuten"}:
        raise ValueError("pair collection requires a supported source")
    if not math.isfinite(time_limit_seconds) or not 0 < time_limit_seconds <= 120:
        raise ValueError("wall-clock limit must be positive and at most 120 seconds")
    if not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 30:
        raise ValueError("request timeout must be positive and at most 30 seconds")
    deadline = time.monotonic() + time_limit_seconds
    client = session if session is not None else requests.Session()
    wrapped: _PairSession | None = None
    try:
        adapter = client.adapters.get("https://")
        if (
            not client.trust_env
            or client.verify is not True
            or not isinstance(adapter, requests.adapters.HTTPAdapter)
            or adapter.max_retries.total != 0
        ):
            raise ValueError("pair collection requires ordinary proxy/TLS and no retries")
        wrapped = _PairSession(client, deadline, before_request, on_response)
        remaining = deadline - time.monotonic()
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
            request_budget=1 if source == "monex" else 2,
            time_limit_seconds=remaining,
            request_timeout_seconds=request_timeout_seconds,
            session=wrapped,
        )
        result = replace(
            result,
            observations=tuple(
                type(observation).model_validate(
                    {
                        **observation.model_dump(exclude={"snapshot_date"}),
                        "fetched_at_utc": wrapped.finished_at_utc,
                    }
                )
                for observation in result.observations
            ),
            requests=wrapped.requests,
            last_response_finished_at_utc=wrapped.finished_at_utc,
            response_content_bytes=wrapped.content_bytes,
        )
        if wrapped.requests == 0:
            if wrapped.reservation_started:
                return replace(result, stop_reason="uncertain_inflight")
            return replace(
                result,
                outcomes=(CollectionOutcome(source, ticker, "not_attempted", result.stop_reason),),
            )
        if any(outcome.status == "incomplete" for outcome in result.outcomes):
            return replace(result, stop_reason="uncertain_inflight")
        if result.outcomes[0].http_status in {403, 429}:
            result = replace(result, stop_reason=result.outcomes[0].status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return replace(result, stop_reason="uncertain_inflight")
        try:
            with _wall_clock_limit(remaining):
                _checkpoint_callback(lambda: on_outcome(result))
        except _TimeLimit:
            return replace(result, stop_reason="uncertain_inflight")
        return result
    finally:
        if wrapped is not None:
            wrapped.close()
        if session is None:
            client.close()


def save_staging(path: Path, observations: Sequence[Observation]) -> None:
    """Atomically create private normalized staging; never overwrite an earlier receipt."""
    payload = {
        "format": STAGING_FORMAT,
        "observations": [
            {
                "kind": "model_valuation"
                if isinstance(item, ModelValuationObservation)
                else "analyst_rating",
                "data": item.model_dump(mode="json"),
            }
            for item in observations
        ],
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        path.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_staging(path: Path) -> tuple[Observation, ...]:
    """Validate saved normalized data without changing original receipt times."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "observations"}
        or payload["format"] != STAGING_FORMAT
        or not isinstance(payload["observations"], list)
    ):
        raise ValueError("invalid retail dynamics staging format")
    observations: list[Observation] = []
    for item in payload["observations"]:
        if not isinstance(item, dict) or set(item) != {"kind", "data"}:
            raise ValueError("invalid staged observation")
        if item["kind"] == "model_valuation":
            observations.append(ModelValuationObservation.model_validate(item["data"]))
        elif item["kind"] == "analyst_rating":
            observations.append(AnalystRatingObservation.model_validate(item["data"]))
        else:
            raise ValueError("unknown staged observation kind")
    return tuple(observations)
