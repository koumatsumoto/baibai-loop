"""判断成果の共通値と構文契約。工程の実行・保存は所有しない。"""
from __future__ import annotations
from datetime import date, datetime
from typing import Annotated
from pydantic import BeforeValidator, ConfigDict, Field, StringConstraints
from baibai_engine.primitives.stocks import normalize_ticker


_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


_TICKER = r"^[0-9A-Z]{4}$"


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _normalize_ticker(value: object) -> object:
    return normalize_ticker(value) if isinstance(value, str) else value


Ticker = Annotated[str, BeforeValidator(_normalize_ticker), Field(pattern=_TICKER)]


_SHA256 = r"^[0-9a-f]{64}$"


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


AssessmentId = Annotated[str, Field(pattern=rf"^stock-assessment-{_UUID}$")]


AssessmentReviewId = Annotated[str, Field(pattern=rf"^assessment-review-{_UUID}$")]


class AssessmentError(ValueError):
    """Raised when an assessment cannot be evaluated without inventing facts."""


def _date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO date string") from error


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value
    if not isinstance(value, str):
        raise ValueError("must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO datetime string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must include a timezone")
    return parsed


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value

