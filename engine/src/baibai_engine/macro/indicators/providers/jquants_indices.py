from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from baibai_engine.foundation.env import load_project_env

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    parse_float,
    record_observation,
)

# jquants_flows と同じ資格情報。indicators 用に別 secret を増やさない。
# Env var name (a credential key, not a secret value); B105 false positive.
_API_KEY_ENV = "JQUANTS_API_KEY"  # nosec B105

# provider_series_id -> ClientV2 の index bars メソッド名。registry に series を
# 持つ index だけを明示登録する。読む経路のない系列を store に入れないため、
# provider が答えられる index の全量ではなくこの表が取得対象を決める。
_INDEX_METHODS: Mapping[str, str] = {
    "topix": "get_idx_bars_daily_topix",
}

# index レベルの妥当域。列取り違え・単位崩れは必ずこの外に落ちる。
_PLAUSIBLE_MIN = 100.0
_PLAUSIBLE_MAX = 20000.0

_DATE_KEYS: tuple[str, ...] = ("Date",)
_CLOSE_KEYS: tuple[str, ...] = ("C", "Close")


class JQuantsIndicesProvider:
    """J-Quants の指数日次バーから終値を返す。指数ごとに専用 endpoint を割り当てる。

    auth/HTTP は jquantsapi.ClientV2 が担うため共有 requests セッションは使わない。
    fetch は認証と取得だけを行い、解析は純粋関数 parse_index_bars に委譲する。
    """

    spec = ProviderSpec(
        name="jquants_indices",
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
        api_key = _read_api_key()
        rows = _fetch_index_bars(api_key, series.provider_series_id, start=start, end=end)
        return parse_index_bars(series, rows, start=start, end=end)


def parse_index_bars(
    series: SeriesDefinition,
    rows: Sequence[Mapping[str, object]],
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    observations_by_date: dict[date, ObservationRecord] = {}
    for row in rows:
        # 日付・終値はレンジ外行も検証して、列が欠けた schema 崩れを必ず捕捉する。
        observed_at = _row_date(row)
        close = _close_value(row)
        if not start <= observed_at <= end:
            continue
        if not _PLAUSIBLE_MIN <= close <= _PLAUSIBLE_MAX:
            raise IndicatorsProviderError(
                f"jquants_indices {series.series_id} close {close} outside plausible "
                f"[{_PLAUSIBLE_MIN}, {_PLAUSIBLE_MAX}]"
            )
        observations_by_date[observed_at] = record_observation(
            series, observed_at=observed_at, value=close
        )
    return [observations_by_date[key] for key in sorted(observations_by_date)]


def _read_api_key() -> str:
    load_project_env()
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise IndicatorsProviderError(
            f"missing {_API_KEY_ENV}; set it in the environment or .env to fetch jquants_indices"
        )
    return token


def _fetch_index_bars(
    api_key: str, provider_series_id: str, *, start: date, end: date
) -> list[Mapping[str, object]]:
    method_name = _INDEX_METHODS.get(provider_series_id)
    if method_name is None:
        supported = ", ".join(sorted(_INDEX_METHODS))
        raise IndicatorsProviderError(
            f"unsupported jquants_indices series {provider_series_id!r}; supported: {supported}"
        )
    try:
        import jquantsapi
    except ModuleNotFoundError as exc:
        raise IndicatorsProviderError("jquantsapi is not installed") from exc
    client = jquantsapi.ClientV2(api_key=api_key)
    method = getattr(client, method_name, None)
    if not callable(method):
        raise IndicatorsProviderError(f"jquantsapi.ClientV2 has no callable {method_name}")
    try:
        frame = method(
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
    except Exception as exc:
        # api_key が例外文字列に混入し得るので redact し、from None で原因チェーンも断つ。
        sanitized = _redact(str(exc), api_key)
        raise IndicatorsProviderError(
            f"failed to fetch jquants_indices {provider_series_id}: "
            f"{type(exc).__name__}: {sanitized}"
        ) from None
    return _frame_to_rows(frame)


def _frame_to_rows(frame: object) -> list[Mapping[str, object]]:
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise IndicatorsProviderError("unexpected jquants_indices payload: not a DataFrame")
    records = to_dict(orient="records")
    if not isinstance(records, list):
        raise IndicatorsProviderError("unexpected jquants_indices payload: records is not a list")
    rows: list[Mapping[str, object]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise IndicatorsProviderError(
                "unexpected jquants_indices payload: row is not a mapping"
            )
        rows.append({str(key): value for key, value in item.items()})
    return rows


def _row_date(row: Mapping[str, object]) -> date:
    raw = _coalesce(row, _DATE_KEYS)
    if raw is None:
        raise IndicatorsProviderError(
            f"jquants_indices row missing a date column; tried {', '.join(_DATE_KEYS)}"
        )
    return _parse_date(raw)


def _close_value(row: Mapping[str, object]) -> float:
    raw = _coalesce(row, _CLOSE_KEYS)
    if raw is None:
        raise IndicatorsProviderError(
            f"jquants_indices row missing a close column; tried {', '.join(_CLOSE_KEYS)}"
        )
    return _coerce_float(raw, column=_CLOSE_KEYS[0])


def _coalesce(row: Mapping[str, object], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if key in row:
            value = row[key]
            if value is not None and value != "":
                return value
    return None


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise IndicatorsProviderError(f"invalid jquants_indices date: {value!r}") from exc
    raise IndicatorsProviderError(f"jquants_indices date column is not date-like: {value!r}")


def _coerce_float(value: object, *, column: str) -> float:
    if isinstance(value, bool):
        raise IndicatorsProviderError(
            f"jquants_indices column {column} is boolean, expected a number"
        )
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        if value != value:  # NaN は欠損なので明示的に弾く
            raise IndicatorsProviderError(f"jquants_indices column {column} is NaN")
        return value
    if isinstance(value, str):
        return parse_float(value)
    raise IndicatorsProviderError(f"jquants_indices column {column} is non-numeric: {value!r}")


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text
