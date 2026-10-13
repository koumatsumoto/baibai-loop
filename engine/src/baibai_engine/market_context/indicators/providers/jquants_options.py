from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Self, cast

from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry

from baibai_engine.foundation.env import load_project_env

from ..definitions import SeriesDefinition
from ..observations import ObservationRecord
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
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
# 3系列・全営業日で共有するprocess内の取得予算。SDK内部の待機と通信時間も数え、
# deadlineに収まらない待機は短縮せず停止する。job残時間の伝達とは別の既存20分上限。
_FETCH_BUDGET_SECONDS = 20 * 60


@dataclass(slots=True)
class _FetchDeadline:
    seconds: float = _FETCH_BUDGET_SECONDS
    expires_at: float | None = None

    def check(self, waiting: float = 0.0) -> None:
        now = time.monotonic()
        if self.expires_at is None:
            self.expires_at = now + self.seconds
        if now + waiting >= self.expires_at:
            raise ProviderFetchStoppedError("jquants_options fetch deadline exhausted")

    def wait(self, seconds: float) -> None:
        self.check(seconds)
        if seconds > 0:
            time.sleep(seconds)


class _OptionsReadRetry(Retry):
    """Retry one GET, retaining the same deadline through urllib3's clones."""

    def __init__(self, *, deadline: _FetchDeadline, **kwargs: Any) -> None:
        self.deadline = deadline
        super().__init__(**kwargs)

    def new(self, **kw: Any) -> Self:
        return super().new(deadline=self.deadline, **kw)

    def get_backoff_time(self) -> float:
        if not self.history:
            return 0.0
        return float(_RATE_LIMIT_BACKOFF_SECONDS[min(len(self.history), 5) - 1])

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        waiting = self.get_backoff_time()
        if response is not None and self.respect_retry_after_header:
            try:
                retry_after = self.get_retry_after(response)
            except InvalidHeader:
                retry_after = None
            if retry_after is not None:
                waiting = retry_after
        self.deadline.wait(waiting)


class _OptionsReadAdapter(HTTPAdapter):
    def __init__(self, original: HTTPAdapter, deadline: _FetchDeadline) -> None:
        self.deadline = deadline
        super().__init__(
            pool_connections=original._pool_connections,
            pool_maxsize=original._pool_maxsize,
            pool_block=original._pool_block,
            max_retries=_OptionsReadRetry(
                deadline=deadline,
                total=5,
                allowed_methods=frozenset({"GET"}),
                status_forcelist=_RETRYABLE_STATUSES,
            ),
        )

    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        self.deadline.check()
        return super().send(request, *args, **kwargs)


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
        self._deadline = _FetchDeadline()
        # A failed chain is also one shared result: another reading must not reset
        # that day's HTTP retry budget. This memo lasts only in this provider process.
        self._failures: dict[date, str] = {}

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
        client = _client(api_key, deadline=self._deadline)
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
        if day in self._failures:
            raise ProviderFetchStoppedError(self._failures[day]) from None
        try:
            self._deadline.check()
            rows = _fetch_chain(client, day, api_key=api_key)
            reading = _reading(rows, day) if rows else None
        except IndicatorsProviderError as exc:
            message = _redact(str(exc), api_key)
            self._failures[day] = message
            raise ProviderFetchStoppedError(message) from None
        self._readings[day] = reading
        return reading


def _reading(rows: Sequence[Mapping[str, object]], day: date) -> FearReadings:
    return fear_readings(quotes_from_records(rows, day), day)


def _reading_field(provider_series_id: str) -> str:
    field = _READING_FIELDS.get(provider_series_id)
    if field is None:
        supported = ", ".join(sorted(_READING_FIELDS))
        raise ProviderFetchStoppedError(
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
        raise ProviderFetchStoppedError(
            f"missing {_API_KEY_ENV}; set it in the environment or .env to fetch jquants_options"
        )
    return token


def _client(api_key: str, *, deadline: _FetchDeadline) -> object:
    try:
        import jquantsapi
    except ModuleNotFoundError as exc:
        raise ProviderFetchStoppedError("jquantsapi is not installed") from exc
    client = jquantsapi.ClientV2(api_key=api_key)
    # ClientV2 2.7 exposes no public transport setting. Keep its ordinary session's
    # TLS/proxy and pool sizing; replace only this client's HTTPS read adapter.
    session = client._request_session()
    original = cast("HTTPAdapter", session.adapters["https://"])
    session.mount("https://", _OptionsReadAdapter(original, deadline))
    original.close()
    return client


def _fetch_chain(client: object, day: date, *, api_key: str) -> list[Mapping[str, object]]:
    method = getattr(client, "get_drv_bars_daily_opt_225", None)
    if not callable(method):
        raise ProviderFetchStoppedError(
            "jquantsapi.ClientV2 has no callable get_drv_bars_daily_opt_225"
        )
    frame = _call(method, day, api_key=api_key)
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise ProviderFetchStoppedError("unexpected jquants_options payload: not a DataFrame")
    records = to_dict(orient="records")
    if not isinstance(records, list):
        raise ProviderFetchStoppedError("unexpected jquants_options payload: records is not a list")
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
            raise ProviderFetchStoppedError(
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
        raise ProviderFetchStoppedError(
            f"jquants_options answered for {seen} when {day.isoformat()} was requested"
        )


def _call(method: object, day: date, *, api_key: str) -> object:
    """Call the SDK method once; pagination and HTTP retries belong to the adapter."""
    assert callable(method)
    try:
        return method(date_yyyymmdd=day.strftime("%Y%m%d"))
    except Exception as exc:
        sanitized = _redact(str(exc), api_key)
        raise ProviderFetchStoppedError(
            f"failed to fetch jquants_options for {day.isoformat()}: "
            f"{type(exc).__name__}: {sanitized}"
        ) from None


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message
