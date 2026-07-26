from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from baibai_engine.foundation.env import load_project_env

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from ..read_contracts import JQUANTS_FLOWS_SPEC
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    parse_float,
)

# screening と同じ資格情報を使う。env 名は screening 側 (ScreeningConfig.from_env が読む
# "JQUANTS_API_KEY") と一致させ、indicators 用に別 secret を増やさない。
_API_KEY_ENV = JQUANTS_FLOWS_SPEC.required_env[0]

# 投資部門別売買状況は市場区分ごとに 1 週 1 行を返す。区分を絞らないと同一週に複数区分が
# 並んで単一時系列にならないため、海外勢フローが最も効く東証プライムに固定する。
_MARKET_SECTION = "TSEPrime"

type ColumnKeys = tuple[str, ...]
type MetricColumns = tuple[ColumnKeys, ColumnKeys, ColumnKeys]

# provider_series_id -> (balance 候補, buy 候補, sell 候補)。ClientV2 (v2 investor-types) の
# 省略名と J-Quants v1 trades_spec の正式名の双方を受ける。net = balance、無ければ buy - sell。
_METRIC_COLUMNS: Mapping[str, MetricColumns] = {
    "foreigners_net_value": (
        ("FrgnBal", "ForeignersBalance", "ForeignBalance"),
        ("FrgnBuy", "ForeignersPurchases", "ForeignPurchases"),
        ("FrgnSell", "ForeignersSales", "ForeignSales"),
    ),
    "individuals_net_value": (
        ("IndBal", "IndividualsBalance"),
        ("IndBuy", "IndividualsPurchases"),
        ("IndSell", "IndividualsSales"),
    ),
    "investment_trusts_net_value": (
        ("InvTrBal", "InvestmentTrustsBalance"),
        ("InvTrBuy", "InvestmentTrustsPurchases"),
        ("InvTrSell", "InvestmentTrustsSales"),
    ),
}

# 集計週末を観測日、公表日を vintage として、週次 fact と利用可能時点を分ける。
_PUBLISHED_DATE_KEYS: ColumnKeys = ("PubDate", "PublishedDate")
_START_DATE_KEYS: ColumnKeys = ("StDate", "StartDate")
_END_DATE_KEYS: ColumnKeys = ("EnDate", "EndDate")


class JQuantsFlowsProvider:
    """J-Quants 投資部門別売買状況 (trades_spec) の投資部門別ネット買い越し額を週次で返す。

    auth/HTTP は jquantsapi.ClientV2 が担うため共有 requests セッションは使わない。fetch は
    認証と取得だけを行い、解析は純粋関数 parse_trades_spec に委譲する。
    """

    spec = JQUANTS_FLOWS_SPEC
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
        rows = _fetch_trades_spec(api_key, start=start, end=end)
        return parse_trades_spec(series, rows, start=start, end=end)


def parse_trades_spec(
    series: SeriesDefinition,
    rows: Sequence[Mapping[str, object]],
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    columns = _METRIC_COLUMNS.get(series.provider_series_id)
    if columns is None:
        supported = ", ".join(sorted(_METRIC_COLUMNS))
        raise IndicatorsProviderError(
            f"unsupported jquants_flows metric {series.provider_series_id!r}; "
            f"supported: {supported}"
        )
    balance_keys, buy_keys, sell_keys = columns
    observations_by_identity: dict[tuple[date, date], ObservationRecord] = {}
    for row in rows:
        # 日付・数値はレンジ外行も検証して、列が欠けた schema 崩れを必ず捕捉する。
        published_at = _row_date(row)
        value = _net_value(
            row,
            balance_keys=balance_keys,
            buy_keys=buy_keys,
            sell_keys=sell_keys,
            metric=series.provider_series_id,
        )
        if start <= published_at <= end:
            period_end = _optional_row_date(row, _END_DATE_KEYS) or published_at
            period_start = _optional_row_date(row, _START_DATE_KEYS) or period_end
            if not start <= period_end <= end:
                continue
            observation = ObservationRecord(
                series_id=series.series_id,
                observed_at=period_end,
                value=value,
                unit=series.unit,
                source_url=series.source_url,
                period_start=period_start,
                period_end=period_end,
                vintage_at=datetime(
                    published_at.year,
                    published_at.month,
                    published_at.day,
                    tzinfo=UTC,
                ),
            )
            identity = (period_end, published_at)
            current = observations_by_identity.get(identity)
            if current is not None:
                if observation != current:
                    raise IndicatorsProviderError(
                        "jquants_flows has conflicting values for the same period and publication"
                    )
                continue
            observations_by_identity[identity] = observation
    return [observations_by_identity[key] for key in sorted(observations_by_identity)]


def _read_api_key() -> str:
    load_project_env()
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise IndicatorsProviderError(
            f"missing {_API_KEY_ENV}; set it in the environment or .env to fetch jquants_flows"
        )
    return token


def _fetch_trades_spec(api_key: str, *, start: date, end: date) -> list[Mapping[str, object]]:
    try:
        import jquantsapi
    except ModuleNotFoundError as exc:
        raise IndicatorsProviderError("jquantsapi is not installed") from exc
    client = jquantsapi.ClientV2(api_key=api_key)
    try:
        frame = client.get_eq_investor_types(
            section=_MARKET_SECTION,
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
    except Exception as exc:
        # api_key が例外文字列に混入し得るので redact し、from None で原因チェーンも断つ。
        sanitized = _redact(str(exc), api_key)
        raise IndicatorsProviderError(
            f"failed to fetch jquants_flows trades_spec: {type(exc).__name__}: {sanitized}"
        ) from None
    return _frame_to_rows(frame)


def _frame_to_rows(frame: object) -> list[Mapping[str, object]]:
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise IndicatorsProviderError("unexpected jquants_flows payload: not a DataFrame")
    records = to_dict(orient="records")
    if not isinstance(records, list):
        raise IndicatorsProviderError("unexpected jquants_flows payload: records is not a list")
    rows: list[Mapping[str, object]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise IndicatorsProviderError("unexpected jquants_flows payload: row is not a mapping")
        rows.append({str(key): value for key, value in item.items()})
    return rows


def _row_date(row: Mapping[str, object]) -> date:
    raw = _coalesce(row, _PUBLISHED_DATE_KEYS)
    if raw is None:
        raw = _coalesce(row, _END_DATE_KEYS)
    if raw is None:
        tried = ", ".join((*_PUBLISHED_DATE_KEYS, *_END_DATE_KEYS))
        raise IndicatorsProviderError(f"jquants_flows row missing a date column; tried {tried}")
    return _parse_date(raw)


def _optional_row_date(row: Mapping[str, object], keys: ColumnKeys) -> date | None:
    raw = _coalesce(row, keys)
    return None if raw is None else _parse_date(raw)


def _net_value(
    row: Mapping[str, object],
    *,
    balance_keys: ColumnKeys,
    buy_keys: ColumnKeys,
    sell_keys: ColumnKeys,
    metric: str,
) -> float:
    balance = _coalesce(row, balance_keys)
    if balance is not None:
        return _coerce_float(balance, column=balance_keys[0])
    buy = _coalesce(row, buy_keys)
    sell = _coalesce(row, sell_keys)
    if buy is None or sell is None:
        tried = ", ".join((*balance_keys, *buy_keys, *sell_keys))
        raise IndicatorsProviderError(f"jquants_flows row missing {metric} columns; tried {tried}")
    return _coerce_float(buy, column=buy_keys[0]) - _coerce_float(sell, column=sell_keys[0])


def _coalesce(row: Mapping[str, object], keys: ColumnKeys) -> object | None:
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
            raise IndicatorsProviderError(f"invalid jquants_flows date: {value!r}") from exc
    raise IndicatorsProviderError(f"jquants_flows date column is not date-like: {value!r}")


def _coerce_float(value: object, *, column: str) -> float:
    if isinstance(value, bool):
        raise IndicatorsProviderError(
            f"jquants_flows column {column} is boolean, expected a number"
        )
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        if value != value:  # NaN は欠損なので明示的に弾く
            raise IndicatorsProviderError(f"jquants_flows column {column} is NaN")
        return value
    if isinstance(value, str):
        return parse_float(value)
    raise IndicatorsProviderError(f"jquants_flows column {column} is non-numeric: {value!r}")


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text
