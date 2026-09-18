"""調査工程へ論理resourceの読取結果と固定keyを見せる。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    MACRO_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)


@dataclass(frozen=True)
class Paths:
    application: Path = APPLICATION_DB_PATH
    macro: Path = MACRO_DB_PATH
    market: Path = MARKET_DB_PATH
    runs: Path = RUNS_DB_PATH
    calibration: Path = CALIBRATION_DIR / "current.sqlite"
    er: Path = ER_LEVEL_CALIBRATION_CONTEXT_PATH


@dataclass(frozen=True)
class Record:
    payload: dict[str, Any]
    identity: dict[str, Any]
    key: list[str | int | float] = field(default_factory=list)


@dataclass(frozen=True)
class Page:
    records: list[Record]
    meta: dict[str, Any] = field(default_factory=dict)


type Get = Callable[[Paths, dict[str, Any]], Page]
type List = Callable[[Paths, dict[str, Any], list[str | int | float] | None, int], Page]


@dataclass(frozen=True)
class ResourceSpec:
    resource_id: str
    layer: str
    domain: str
    authority: str
    description: str
    selector: type[BaseModel]
    identity: type[BaseModel]
    filters: type[BaseModel]
    sort: tuple[str, ...]
    get: Get | None
    page: List | None
    list_shape: str = "rows"
    time_basis: str = "stored"
    payload_schema: str = "stored row"


def record(row: dict[str, Any], identity: tuple[str, ...], sort: tuple[str, ...]) -> Record:
    return Record(
        {k: v for k, v in row.items() if k != "page_time"},
        {k: row[k] for k in identity},
        [row[k] for k in sort],
    )
