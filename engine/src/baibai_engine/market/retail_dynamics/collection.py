"""Serial bounded acquisition into private normalized staging, never publication."""

from __future__ import annotations

import json
import math
import re
import signal
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal

import requests
from pydantic import ValidationError

from baibai_engine.market.ticker import normalize_ticker

from .models import AnalystRatingObservation, ModelValuationObservation, Observation
from .sources import ParseError, monex, rakuten

DEFAULT_TICKERS = ("7203", "9432", "6758")
MAX_TICKERS = 3
MAX_REQUESTS = 9
STAGING_FORMAT = "retail-dynamics-staging-v1"
type Source = Literal["monex", "rakuten"]


@dataclass(frozen=True)
class CollectionOutcome:
    source: Source
    ticker: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    observations: tuple[Observation, ...]
    outcomes: tuple[CollectionOutcome, ...]
    requests: int
    stop_reason: str | None = None

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

    def receive(url: str) -> tuple[str, datetime]:
        nonlocal used
        if used >= request_budget:
            raise _RequestLimit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _TimeLimit
        used += 1
        return _receive(client, url, min(request_timeout_seconds, remaining))

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
                        outcomes.append(CollectionOutcome(source, ticker, "observed"))
                    except _HttpFailure as exc:
                        category = (
                            "access_denied"
                            if exc.status == 403
                            else "rate_limited"
                            if exc.status == 429
                            else "http_failure"
                        )
                        outcomes.append(CollectionOutcome(source, ticker, category))
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
                    except (ParseError, ValidationError):
                        outcomes.append(CollectionOutcome(source, ticker, "parse_failure"))
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


def save_staging(path: Path, observations: Sequence[Observation]) -> None:
    """Save only typed normalized observations in a private atomic local file."""
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
        temporary.replace(path)
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
