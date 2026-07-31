from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from baibai_engine.foundation.env import load_project_env

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    record_observation,
)
from .option_iv import FearReadings, fear_readings, quotes_from_records

# jquants_flows / jquants_indices と同じ資格情報。indicators 用に secret を増やさない。
# Env var name (a credential key, not a secret value); B105 false positive.
_API_KEY_ENV = "JQUANTS_API_KEY"  # nosec B105

# provider_series_id -> FearReadings の属性名。registry に series を持つ読みだけを
# 登録する。読む経路のない値を store に入れないため、この表が取得対象を決める。
_READING_FIELDS: Mapping[str, str] = {
    "n225_iv_30d": "iv_30d",
    "n225_iv_skew": "iv_skew",
    "n225_iv_term": "iv_term",
}

# ボラティリティ点の妥当域。水準系 (iv_30d) は下限を持ち、差分系 (skew / term) は
# 符号が両方向に出るので別の域を持つ。列取り違えや比率と % の取り違えは必ず外へ落ちる。
_LEVEL_RANGE = (3.0, 200.0)
_SPREAD_RANGE = (-100.0, 100.0)


class JQuantsOptionsProvider:
    """日経225オプションのチェーンを恐怖の読みへ集計して返す。

    endpoint は 1 日 1 断面しか返さないので、range 取得は日次ループになる。
    チェーン自体 (1 日 1 万行) は保存せず、集計後の系列だけを observation にする。
    fetch は認証と取得だけを行い、集計は純粋関数 option_iv に委譲する。
    """

    spec = ProviderSpec(
        name="jquants_options",
        all_history_rolling_years=10,
        required_env=(_API_KEY_ENV,),
    )
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        del session, context
        field = _reading_field(series.provider_series_id)
        api_key = _read_api_key()
        client = _client(api_key)
        observations: list[ObservationRecord] = []
        for day in _days(start, end):
            rows = _fetch_chain(client, day, api_key=api_key)
            if not rows:
                # A closed market returns nothing; that is the day's fact, not a
                # failure, and skipping keeps the series free of invented points.
                continue
            reading = _reading(rows, day)
            value = getattr(reading, field)
            if value is None:
                continue
            _require_plausible(series, field, value)
            observations.append(record_observation(series, observed_at=day, value=value))
        return observations


def _reading(rows: Sequence[Mapping[str, object]], day: date) -> FearReadings:
    return fear_readings(quotes_from_records(rows, day), day)


def _reading_field(provider_series_id: str) -> str:
    field = _READING_FIELDS.get(provider_series_id)
    if field is None:
        supported = ", ".join(sorted(_READING_FIELDS))
        raise IndicatorsProviderError(
            f"unsupported jquants_options series {provider_series_id!r}; supported: {supported}"
        )
    return field


def _require_plausible(series: SeriesDefinition, field: str, value: float) -> None:
    low, high = _LEVEL_RANGE if field == "iv_30d" else _SPREAD_RANGE
    if not low <= value <= high:
        raise IndicatorsProviderError(
            f"jquants_options {series.series_id} value {value} outside plausible [{low}, {high}]"
        )


def _days(start: date, end: date) -> list[date]:
    if start > end:
        return []
    # Weekends never carry a chain, so skipping them removes two empty calls in
    # seven from a multi-year backfill without needing a holiday calendar.
    return [
        day
        for offset in range((end - start).days + 1)
        if (day := start + timedelta(days=offset)).weekday() < 5
    ]


def _read_api_key() -> str:
    load_project_env()
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise IndicatorsProviderError(
            f"missing {_API_KEY_ENV}; set it in the environment or .env to fetch jquants_options"
        )
    return token


def _client(api_key: str) -> object:
    try:
        import jquantsapi
    except ModuleNotFoundError as exc:
        raise IndicatorsProviderError("jquantsapi is not installed") from exc
    return jquantsapi.ClientV2(api_key=api_key)


def _fetch_chain(client: object, day: date, *, api_key: str) -> list[Mapping[str, object]]:
    method = getattr(client, "get_drv_bars_daily_opt_225", None)
    if not callable(method):
        raise IndicatorsProviderError(
            "jquantsapi.ClientV2 has no callable get_drv_bars_daily_opt_225"
        )
    try:
        frame = method(date_yyyymmdd=day.strftime("%Y%m%d"))
    except Exception as exc:
        # api_key が例外文字列に混入し得るので redact し、from None で原因チェーンも断つ。
        sanitized = _redact(str(exc), api_key)
        raise IndicatorsProviderError(
            f"failed to fetch jquants_options for {day.isoformat()}: "
            f"{type(exc).__name__}: {sanitized}"
        ) from None
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise IndicatorsProviderError("unexpected jquants_options payload: not a DataFrame")
    records = to_dict(orient="records")
    if not isinstance(records, list):
        raise IndicatorsProviderError("unexpected jquants_options payload: records is not a list")
    return [record for record in records if isinstance(record, Mapping)]


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message
