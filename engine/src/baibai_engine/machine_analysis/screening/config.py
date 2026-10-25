from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR as DEFAULT_SQLITE_CACHE_DIR


class ConfigError(ValueError):
    """Raised when required screening configuration is missing."""


class ScreeningConfig(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    jquants_api_key: str | None = Field(default=None, min_length=1)
    edinet_api_key: str | None = None
    sqlite_cache_dir: Path = DEFAULT_SQLITE_CACHE_DIR

    def __init__(
        self,
        jquants_api_key: str | None = None,
        edinet_api_key: str | None = None,
        /,
        **data: Any,
    ) -> None:
        if jquants_api_key is not None:
            data["jquants_api_key"] = jquants_api_key
        if edinet_api_key is not None:
            data["edinet_api_key"] = edinet_api_key
        super().__init__(**data)

    @field_validator("sqlite_cache_dir", mode="before")
    @classmethod
    def _coerce_dir(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value:
            return Path(value)
        raise ValueError("path must be a non-empty string or Path")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ScreeningConfig:
        source = env if env is not None else os.environ
        # sqlite_cache_dir は repo 内の運用規約に固定する。
        # env override は作業者ごとの local store 分岐を生むため受け付けない。
        sqlite_cache_dir_value = str(DEFAULT_SQLITE_CACHE_DIR)
        try:
            return cls(
                jquants_api_key=source.get("JQUANTS_API_KEY") or None,
                edinet_api_key=source.get("EDINET_API_KEY") or None,
                sqlite_cache_dir=sqlite_cache_dir_value,
            )
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
