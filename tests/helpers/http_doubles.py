"""Finite HTTP responses, sessions and body failures for offline provider tests.

Response fields and recorded requests let callers assert both the received payload
and the request that produced it. Specialized streaming doubles stay with their
transport fixtures when their contracts differ.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from urllib3.exceptions import ReadTimeoutError

_UNSET = object()


class BodyTimeout(io.BytesIO):
    def read(self, *args, **kwargs):
        raise ReadTimeoutError(None, None, "synthetic body timeout")


@dataclass
class FakeResponse:
    """What `requests` hands back: a status, bytes, headers, and maybe JSON."""

    status_code: int = 200
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    _payload: Any = None
    close_count: int = 0

    def close(self) -> None:
        self.close_count += 1

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("this response carries no JSON body")
        return self._payload

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


def response(
    content: bytes = b"",
    *,
    status_code: int = 200,
    content_type: str | None = None,
    payload: Any = None,
) -> FakeResponse:
    """One response; tests can add further headers such as Retry-After."""

    headers = {} if content_type is None else {"content-type": content_type}
    return FakeResponse(status_code=status_code, content=content, headers=headers, _payload=payload)


def html_response(content: bytes) -> FakeResponse:
    return response(content, content_type="text/html; charset=UTF-8")


def json_response(payload: Any, *, status_code: int = 200) -> FakeResponse:
    return response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status_code,
        payload=payload,
    )


class FakeSession:
    """Answers `get` from a queue, from a URL map, or with one fixed response.

    An entry that is an exception is raised instead of returned, which is how a
    transient network failure is expressed. `calls` records every URL in order.
    """

    def __init__(
        self,
        *answers: FakeResponse | BaseException,
        by_url: Mapping[str, FakeResponse | BaseException] | None = None,
        default: FakeResponse | BaseException | None = None,
    ) -> None:
        self._queue = list(answers)
        self._by_url = dict(by_url or {})
        self._default = default
        self.calls: list[str] = []

    def get(
        self, url: str, timeout: int | None = None, *, allow_redirects: bool = True
    ) -> FakeResponse:
        del timeout, allow_redirects
        self.calls.append(url)
        answer: Any = _UNSET
        if url in self._by_url:
            answer = self._by_url[url]
        elif self._queue:
            answer = self._queue.pop(0)
        elif self._default is not None:
            answer = self._default
        if answer is _UNSET:
            raise AssertionError(f"no fake response left for {url}")
        if isinstance(answer, BaseException):
            raise answer
        return answer


def always(answer: FakeResponse | BaseException) -> FakeSession:
    """A session that answers every request the same way."""

    return FakeSession(default=answer)


def by_url(answers: Mapping[str, FakeResponse | BaseException]) -> FakeSession:
    return FakeSession(by_url=answers)


def sequence(answers: Iterable[FakeResponse | BaseException]) -> FakeSession:
    return FakeSession(*answers)


class ExplodingSession:
    """A session whose whole point is that nothing calls it."""

    def __init__(self, reason: str = "session.get must not be called") -> None:
        self._reason = reason
        self.calls: list[str] = []

    def get(
        self, url: str, timeout: int | None = None, *, allow_redirects: bool = True
    ) -> FakeResponse:
        del timeout, allow_redirects
        self.calls.append(url)
        raise AssertionError(self._reason)
