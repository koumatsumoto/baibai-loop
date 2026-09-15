"""Research の L1 読取入力・資源上限を定義し、曖昧な分析結果を止める。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.market.lake.keys import validate_identifier, validate_sha256

type Scalar = str | int | float | bool | None
type ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "BUSY",
    "RELEASE_UNAVAILABLE",
    "CONTRACT_MISMATCH",
    "INTEGRITY_ERROR",
    "TRANSFER_BUDGET_EXCEEDED",
    "QUERY_LIMIT_EXCEEDED",
    "QUERY_TIMEOUT",
    "QUERY_REJECTED",
    "RESULT_TOO_LARGE",
    "UNSUPPORTED_RESULT_TYPE",
    "UPSTREAM_UNAVAILABLE",
]


class L1Error(Exception):
    """外部例外の本文を運ばない、公開用のエラー分類。"""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    sources: int = 6
    query_bytes: int = 256 * 1024 * 1024
    process_gets: int = 1000
    process_bytes: int = 2 * 1024 * 1024 * 1024
    sql_bytes: int = 16 * 1024
    parameter_bytes: int = 16 * 1024
    sql_seconds: float = 30
    max_rows: int = 2000
    result_bytes: int = 256 * 1024
    duckdb_memory_bytes: int = 512 * 1024 * 1024
    child_memory_bytes: int = 2 * 1024 * 1024 * 1024
    http_seconds: float = 30


LIMITS = Limits()


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReleaseRef(InputModel):
    release_id: str
    manifest_sha256: str

    @field_validator("release_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_identifier(value, label="release_id")

    @field_validator("manifest_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        return validate_sha256(value)


class Source(InputModel):
    dataset: str
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    from_date: str = Field(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str = Field(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
    columns: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def valid_range(self) -> Source:
        if date.fromisoformat(self.from_date) > date.fromisoformat(self.to_date):
            raise ValueError("from must not follow to")
        if self.columns is not None and len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be unique")
        return self


class Query(InputModel):
    release_ref: ReleaseRef
    sources: list[Source] = Field(min_length=1, max_length=LIMITS.sources)
    sql: str = Field(min_length=1)
    parameters: dict[Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")], Scalar] = Field(
        default_factory=dict
    )
    max_rows: int = Field(default=1000, ge=1, le=LIMITS.max_rows)

    @model_validator(mode="after")
    def bounded(self) -> Query:
        if len({source.alias for source in self.sources}) != len(self.sources):
            raise ValueError("aliases must be unique")
        if len(self.sql.encode()) > LIMITS.sql_bytes:
            raise ValueError("SQL too large")
        if any(isinstance(v, float) and not math.isfinite(v) for v in self.parameters.values()):
            raise ValueError("nonfinite parameter")
        if len(wire(self.parameters)) > LIMITS.parameter_bytes:
            raise ValueError("parameters too large")
        return self


def wire(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
