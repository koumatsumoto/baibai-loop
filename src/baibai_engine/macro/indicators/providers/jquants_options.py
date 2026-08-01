from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RetryError, Timeout

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
from .option_iv import (
    FearReadings,
    as_date,
    fear_readings,
    quotes_from_records,
    settlement_rows,
)

# jquants_flows / jquants_indices と同じ資格情報。indicators 用に secret を増やさない。
_API_KEY_ENV = "JQUANTS_API_KEY"

# provider_series_id -> FearReadings の属性名。registry に series を持つ読みだけを
# 登録する。読む経路のない値を store に入れないため、この表が取得対象を決める。
_READING_FIELDS: Mapping[str, str] = {
    "n225_iv_30d": "iv_30d",
    "n225_iv_skew": "iv_skew",
    "n225_iv_term": "iv_term",
}

# J-Quants は連続した日次呼び出しに 429 を返す。この endpoint は 1 営業日 1 呼び出し
# なので長い range 取得ほど拒否域に入りやすく、market 側と同じ形の待避を持たせる。
# rate window は数分に及ぶことがあるため末尾は 10 分まで伸ばす。
_RATE_LIMIT_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# 1 process が待避に費やせる総時間。日次 batch の 1 pass は 3 系列 x service の 2 回
# 再試行 = 6 fetch を回すので、fetch ごとの予算では上限が 6 倍になり、拒否が続けば
# 111 分眠って job timeout を超える——cancel は macro の失敗でなく、その日の
# screening publish ごと失われることを意味する。予算は provider が持ち、pass 全体で
# 1 つとする。使い切ったあとは待たずに落ちる: 数時間待っても拒否は明けない。
_BACKOFF_BUDGET_SECONDS = 20 * 60


@dataclass(slots=True)
class _BackoffBudget:
    """How much waiting this process may still spend on rate limits."""

    remaining_seconds: float

    def spend(self, seconds: float) -> bool:
        """Wait, or report that the budget cannot cover this wait."""
        if seconds > self.remaining_seconds:
            return False
        self.remaining_seconds -= seconds
        time.sleep(seconds)
        return True


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

    def __init__(self) -> None:
        # 3 系列は同じチェーンから読むので、1 日 1 回の取得を系列間で共有する。共有
        # しないと全期間取得の呼び出し数が 3 倍になる。過去日のチェーンは変わらない
        # ので memo でよい。休場日 (年 16 日ほど) の None も憶える——憶えないと 3 系列
        # がそれぞれ空のチェーンを取りに行く。
        self._readings: dict[date, FearReadings | None] = {}
        # 待避の予算は series でも fetch でもなく provider が持つ。service は fetch を
        # 2 回試み、1 pass は 3 系列を回すので、fetch ごとに配ると上限が 6 倍になる。
        self._budget = _BackoffBudget(_BACKOFF_BUDGET_SECONDS)

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
            reading = self._day_reading(client, day, api_key=api_key)
            if reading is None:
                # A closed market returns nothing; that is the day's fact, not a
                # failure, and skipping keeps the series free of invented points.
                continue
            value = getattr(reading, field)
            if value is None:
                continue
            observations.append(record_observation(series, observed_at=day, value=value))
        return observations

    def _day_reading(self, client: object, day: date, *, api_key: str) -> FearReadings | None:
        if day in self._readings:
            return self._readings[day]
        rows = _fetch_chain(client, day, api_key=api_key, budget=self._budget)
        reading = _reading(rows, day) if rows else None
        self._readings[day] = reading
        return reading


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


def _fetch_chain(
    client: object, day: date, *, api_key: str, budget: _BackoffBudget
) -> list[Mapping[str, object]]:
    method = getattr(client, "get_drv_bars_daily_opt_225", None)
    if not callable(method):
        raise IndicatorsProviderError(
            "jquantsapi.ClientV2 has no callable get_drv_bars_daily_opt_225"
        )
    frame = _call_with_backoff(method, day, api_key=api_key, budget=budget)
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise IndicatorsProviderError("unexpected jquants_options payload: not a DataFrame")
    records = to_dict(orient="records")
    if not isinstance(records, list):
        raise IndicatorsProviderError("unexpected jquants_options payload: records is not a list")
    rows = [record for record in records if isinstance(record, Mapping)]
    _require_requested_day(rows, day)
    # The duplicate check speaks about the snapshot the readings use. Run against the
    # raw payload it would instead refuse every session the exchange called emergency
    # margin on, where a second full copy of the chain is the documented answer.
    _require_one_row_per_contract(settlement_rows(rows), day)
    return rows


def _require_one_row_per_contract(rows: Sequence[Mapping[str, object]], day: date) -> None:
    """Refuse a chain that carries a contract twice within one snapshot.

    Every reading resolves a strike to one volatility by writing into a dict, so a
    second row for the same contract wins on arrival order and nothing downstream can
    see it happened — the basis check takes a median and a single duplicated strike
    does not move it. The one repetition the source is known to produce is the second
    snapshot of an emergency-margin session, which is separated before this runs; a
    contract repeated inside the settlement snapshot is the source doing something this
    code has not been shown, and guessing which row to keep is worse than stopping.
    """
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (str(row.get("SQD")), str(row.get("Strike")), str(row.get("PCDiv")))
        if key in seen:
            raise IndicatorsProviderError(
                f"jquants_options returned {day.isoformat()} twice for the same contract "
                f"(expiry {key[0]}, strike {key[1]}, side {key[2]})"
            )
        seen.add(key)


def _require_requested_day(rows: Sequence[Mapping[str, object]], day: date) -> None:
    """Refuse a chain that answers for a day other than the one asked for.

    The observation is stamped with the requested date, so a session served the
    previous day's chain would carry the previous day's fear under today's date with
    nothing to show for it. The payload names the day it belongs to; reading it costs
    one comparison and turns a silent re-dating into a failure.
    """
    answered = {parsed for row in rows if (parsed := as_date(row.get("Date"))) is not None}
    if answered and answered != {day}:
        seen = ", ".join(sorted(value.isoformat() for value in sorted(answered))[:3])
        raise IndicatorsProviderError(
            f"jquants_options answered for {seen} when {day.isoformat()} was requested"
        )


def _call_with_backoff(
    method: object, day: date, *, api_key: str, budget: _BackoffBudget
) -> object:
    """Call the endpoint, waiting out a rate limit rather than ending the range.

    Without this a single 429 discards every day already fetched, and a range long
    enough to be worth fetching is long enough to meet one. Errors that will not
    change on a retry (auth, a bad date) fail on the first attempt, and the waiting
    stops once the run has spent its budget — a pass where every day waits the full
    ladder would otherwise outlast any job that scheduled it.
    """
    assert callable(method)
    last: Exception | None = None
    for delay_seconds in (0, *_RATE_LIMIT_BACKOFF_SECONDS):
        if delay_seconds and not budget.spend(delay_seconds):
            break
        try:
            return method(date_yyyymmdd=day.strftime("%Y%m%d"))
        except Exception as exc:
            last = exc
            if not _is_retryable(exc):
                break
    # api_key が例外文字列に混入し得るので redact し、from None で原因チェーンも断つ。
    sanitized = _redact(str(last), api_key)
    raise IndicatorsProviderError(
        f"failed to fetch jquants_options for {day.isoformat()}: {type(last).__name__}: {sanitized}"
    ) from None


def _is_retryable(exc: Exception) -> bool:
    """Whether waiting could change the answer.

    The client retries the rate-limited statuses inside its own session and, once its
    attempts run out, raises a RetryError carrying no response at all — so a status
    code is exactly what a 429 never arrives with, and classifying on the status alone
    made the wait unreachable for the one case it exists for. The exceptions the
    library raises directly (auth, a date outside the published history) do carry a
    response, and those are the ones a retry cannot change.
    """
    if isinstance(exc, RetryError | RequestsConnectionError | Timeout):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUSES


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message
