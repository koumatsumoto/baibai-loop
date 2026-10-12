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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import FrameType
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import requests
from pydantic import ValidationError

from baibai_engine.external_assessment.acquisition.sources import (
    ParseError,
    kabumap,
    monex,
    rakuten,
)
from baibai_engine.external_assessment.observations.identity import (
    PAIR_ACQUISITION_POLICY_VERSION,
    STAGING_FORMAT,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
    Observation,
    QuantsScoreObservation,
)
from baibai_engine.market.ticker import normalize_ticker

DEFAULT_TICKERS = ("7203", "9432", "6758")
MAX_TICKERS = 3
MAX_REQUESTS = 9
MIN_REQUEST_INTERVAL_SECONDS = 2.0
DEFAULT_PAIR_TIME_LIMIT_SECONDS = 120
DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS = 30
# Increment this policy version whenever pair acquisition semantics change.
HOST_REQUEST_RATE_PER_SECOND = 2.0
HOST_REQUEST_BURST_CREDITS = 4.0
RATE_LIMITED_HOSTS = (
    "scouter.monex.co.jp",
    "www.rakuten-sec.co.jp",
    "rakuten-sec.ifis.co.jp",
    "jp.kabumap.com",
)


@dataclass(frozen=True, slots=True)
class ReceivedHTML:
    """Public HTTP 200 body and its decoding basis, without response headers."""

    url: str
    fetched_at_utc: datetime
    encoding: str
    body: bytes = field(repr=False)


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
        "missing_quants_date",
        "missing_quants_scores",
        "changed_quants_score",
    }
)
type Source = Literal["monex", "rakuten", "kabumap"]


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


class HostRateLimiter:
    """Fixed per-host token buckets shared by serial daily pairs and retries.

    All hosts start with zero credits. A new cohort/process loses unused credits;
    it never creates restart burst credit. Actual acquisition stays serial (one
    request at a time), within the requested maximum concurrency of two.
    """

    def __init__(self) -> None:
        started = time.monotonic()
        self._credits = dict.fromkeys(RATE_LIMITED_HOSTS, 0.0)
        self._updated = dict.fromkeys(RATE_LIMITED_HOSTS, started)

    def wait(self, url: str | bytes, *, deadline: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise ValueError("host rate limiter requires the serial main thread")
        if not isinstance(url, str):
            raise ValueError("host rate limiter requires a fixed public HTTPS host")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("host rate limiter requires a fixed public HTTPS host") from None
        if parsed.scheme != "https" or parsed.netloc not in RATE_LIMITED_HOSTS:
            raise ValueError("host rate limiter requires a fixed public HTTPS host")
        host = parsed.netloc
        rate = HOST_REQUEST_RATE_PER_SECOND
        burst = HOST_REQUEST_BURST_CREDITS
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise _TimeLimit
            credits = min(
                burst,
                self._credits[host] + (now - self._updated[host]) * rate,
            )
            self._updated[host] = now
            self._credits[host] = credits
            if credits >= 1.0:
                self._credits[host] -= 1.0
                return
            time.sleep(min((1.0 - credits) / rate, remaining))


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


def _http_retry_date(value: str, received: datetime) -> tuple[datetime | None, bool]:
    weekday = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
    month = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    clock = r"[0-9]{2}:[0-9]{2}:(?P<second>[0-9]{2})"
    imf = re.fullmatch(rf"{weekday}, [0-9]{{2}} {month} [0-9]{{4}} {clock} GMT", value)
    rfc850 = re.fullmatch(
        rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        rf"[0-9]{{2}}-{month}-(?P<year>[0-9]{{2}}) {clock} GMT",
        value,
    )
    asctime = re.fullmatch(rf"{weekday} {month} (?:[0-9]{{2}}| [0-9]) {clock} [0-9]{{4}}", value)
    matched = imf or rfc850 or asctime
    if matched is None:
        return None, False
    leap_second = matched["second"] == "60"
    if leap_second:
        # Validate the remaining calendar fields with the adjacent representable
        # second, then fail closed: datetime cannot represent an HTTP leap second.
        value = value[: matched.start("second")] + "59" + value[matched.end("second") :]
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if asctime:
        date = date.replace(tzinfo=UTC)
    if rfc850:
        # HTTP's fifty-year window is relative to receipt, not email's fixed
        # 1969/2068 pivot. Compare full dates to cover the boundary within a year.
        year = received.year // 100 * 100 + int(rfc850["year"])
        suffix = (date.month, date.day, date.hour, date.minute, date.second, 0)
        received_suffix = (
            received.month,
            received.day,
            received.hour,
            received.minute,
            received.second,
            received.microsecond,
        )
        if (year, *suffix) < (received.year - 50, *received_suffix):
            year += 100
        if (year, *suffix) > (received.year + 50, *received_suffix):
            year -= 100
        if not 1 <= year <= 9999:
            return None, True
        try:
            date = date.replace(year=year)
        except ValueError:
            return None, False
    if leap_second:
        return None, True
    return max(received, date.astimezone(UTC)), False


def _retry_after(value: str | None, received: datetime) -> tuple[datetime | None, bool]:
    """Parse a confirmed 429 deadline; never shorten a valid overflowing delay."""
    if value is None:
        return None, False
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        # Compare decimal strings before int() so arbitrarily large valid delays
        # fail closed rather than hitting Python's integer-string size limit.
        digits = value.lstrip("0") or "0"
        available = datetime.max.replace(tzinfo=UTC) - received
        maximum = str(available.days * 86400 + available.seconds)
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            return None, True
        return received + timedelta(seconds=int(digits)), False
    return _http_retry_date(value, received)


class _HttpFailure(Exception):
    def __init__(
        self,
        status: int,
        retry_after_not_before_utc: datetime | None = None,
        *,
        retry_after_unrepresentable: bool = False,
    ) -> None:
        self.status = status
        self.retry_after_not_before_utc = retry_after_not_before_utc
        self.retry_after_unrepresentable = retry_after_unrepresentable


def _receive(session: requests.Session, url: str, timeout: float) -> tuple[str, datetime]:
    # Redirects would add unbudgeted requests and can redirect to login pages.
    with session.get(url, timeout=timeout, allow_redirects=False) as response:
        if response.status_code != 200:
            if response.status_code == 429:
                # PairSession records full body completion before a possibly
                # slow on_response callback. Do not add its delay.
                received = (
                    session.finished_at_utc
                    if isinstance(session, _PairSession)
                    else datetime.now(UTC)
                )
                assert received is not None
                deadline, unrepresentable = _retry_after(
                    response.headers.get("Retry-After"), received
                )
                raise _HttpFailure(429, deadline, retry_after_unrepresentable=unrepresentable)
            raise _HttpFailure(response.status_code)
        body = response.content
        received = (
            session.finished_at_utc if isinstance(session, _PairSession) else datetime.now(UTC)
        )
        assert received is not None
        content_type = response.headers.get("Content-Type", "")
        charset = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.I)
        meta = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", body[:4096], re.I)
        encoding = charset[1] if charset else meta[1].decode("ascii") if meta else "utf-8"
        callback = session.on_received_html if isinstance(session, _PairSession) else None
        if callback is not None:
            prepared = cast("requests.PreparedRequest | None", response.request)
            if prepared is not None and "Authorization" in prepared.headers:
                raise RuntimeError("raw_capture_requires_anonymous_response")
            callback(ReceivedHTML(url, received, encoding, body))
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
        or any(source not in {"monex", "rakuten", "kabumap"} for source in sources)
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
        if last_request_finished is not None and not (
            isinstance(client, _PairSession) and client.rate_limiter is not None
        ):
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
                        elif source == "kabumap":
                            url = kabumap.public_url(ticker)
                            html, received = receive(url)
                            observation = kabumap.parse_html(
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


def collector_binding() -> str:
    """Bind a resumable plan to acquisition policy and the actual parser versions."""
    policy = {
        "format": PAIR_ACQUISITION_POLICY_VERSION,
        "monex_parser": monex.PARSER_VERSION,
        "rakuten_parser": rakuten.PARSER_VERSION,
        "kabumap_parser": kabumap.PARSER_VERSION,
        "request_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
        "initial_pair_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
        "pair_time_limit_seconds": DEFAULT_PAIR_TIME_LIMIT_SECONDS,
        "request_timeout_seconds": DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
        "source_request_limits": {"monex": 1, "rakuten": 2, "kabumap": 1},
        "daily_host_rate": {
            "hosts": RATE_LIMITED_HOSTS,
            "requests_per_second": HOST_REQUEST_RATE_PER_SECOND,
            "burst_credits": HOST_REQUEST_BURST_CREDITS,
            "initial_credits": 0,
            "actual_concurrency": 1,
            "maximum_concurrency": 2,
        },
    }
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class _PairSession(requests.Session):
    def __init__(
        self,
        client: requests.Session,
        deadline: float,
        on_response: Callable[[datetime, int, int], None] | None,
        rate_limiter: HostRateLimiter | None = None,
        on_received_html: Callable[[ReceivedHTML], None] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.deadline = deadline
        self.on_response = on_response
        self.rate_limiter = rate_limiter
        self.on_received_html = on_received_html
        self.initial_wait_until = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS
        self.requests = 0
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
        if self.requests == 0 and self.rate_limiter is None:
            # A fresh two-second wait also covers immediate process/cohort
            # resumes without inferring elapsed time from a persisted clock.
            while (wait := self.initial_wait_until - time.monotonic()) > 0:
                time.sleep(min(wait, self._remaining()))
        self._remaining()
        if self.rate_limiter is not None:
            # Charge actual send time; all buckets start with no restart credit.
            self.rate_limiter.wait(url, deadline=self.deadline)
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
                callback(finished, self.requests, len(body))
            return response
        except BaseException:
            response.close()
            raise


def collect_pair(
    ticker: str,
    *,
    source: Source,
    on_response: Callable[[datetime, int, int], None] | None = None,
    on_received_html: Callable[[ReceivedHTML], None] | None = None,
    time_limit_seconds: float = DEFAULT_PAIR_TIME_LIMIT_SECONDS,
    request_timeout_seconds: float = DEFAULT_PAIR_REQUEST_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
    rate_limiter: HostRateLimiter | None = None,
) -> CollectionResult:
    """Acquire one fixed pair attempt; retry and cohort commit belong to the caller.

    No HTTP request reserves durable state. The caller may repeat uncommitted
    GETs after a process interruption. Ordinary TLS/proxy, serial host limits,
    finite pair/HTTP deadlines and source identity checks remain unchanged.
    """
    ticker = normalize_ticker(ticker)
    if source not in {"monex", "rakuten", "kabumap"}:
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
        wrapped = _PairSession(client, deadline, on_response, rate_limiter, on_received_html)
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
            request_budget=2 if source == "rakuten" else 1,
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
            return replace(
                result,
                outcomes=(CollectionOutcome(source, ticker, "not_attempted", result.stop_reason),),
            )
        if any(outcome.status == "incomplete" for outcome in result.outcomes):
            return replace(
                result,
                outcomes=(CollectionOutcome(source, ticker, "request_timeout"),),
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


def save_staging(path: Path, observations: Sequence[Observation]) -> None:
    """Atomically create private normalized staging; never overwrite an earlier receipt."""
    payload = {
        "format": STAGING_FORMAT,
        "observations": [
            {
                "kind": "model_valuation"
                if isinstance(item, ModelValuationObservation)
                else "quants_score"
                if isinstance(item, QuantsScoreObservation)
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
        raise ValueError("invalid external assessment staging format")
    observations: list[Observation] = []
    for item in payload["observations"]:
        if not isinstance(item, dict) or set(item) != {"kind", "data"}:
            raise ValueError("invalid staged observation")
        if item["kind"] == "model_valuation":
            observations.append(ModelValuationObservation.model_validate(item["data"]))
        elif item["kind"] == "analyst_rating":
            observations.append(AnalystRatingObservation.model_validate(item["data"]))
        elif item["kind"] == "quants_score":
            observations.append(QuantsScoreObservation.model_validate(item["data"]))
        else:
            raise ValueError("unknown staged observation kind")
    return tuple(observations)
