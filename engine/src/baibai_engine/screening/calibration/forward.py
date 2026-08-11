"""Calendar-aware, fail-visible price-return forward observations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

from baibai_engine.market.bars import asof_basis_closes
from baibai_engine.market.benchmark import TOPIX_ETF_PROXY

from ..providers.jquants import JQuantsDailyBar
from .horizons import HORIZONS, HorizonSpec, require_horizon

__all__ = (
    "HORIZONS",
    "ControlEventExit",
    "ForwardReturnRow",
    "compute_forward_returns",
    "read_control_event_exits",
)

ForwardStatus = str
AdjustmentCoverage = str
TotalReturnStatus = str
TOTAL_RETURN_BASIS = "fy_actual_dividend_fiscal_year_end_window"
TOTAL_RETURN_STATUSES = frozenset(
    {
        "resolved",
        "unresolved_price_return",
        "unresolved_adjustment_factor",
        "unresolved_no_fy_observation",
        "unresolved_missing_dividend",
        "unresolved_invalid_dividend",
        "unresolved_invalid_total_return",
        # 会計期間に分割・併合が入り、支払ごとの換算でも株式基準を確定できない年度。
        # 総リターンは配当を含む定義なので、基準の分からない配当を足した値を resolved と
        # 名乗らせない。この窓は総リターンを読む metric の標本から外れる。
        "unresolved_dividend_split_basis",
    }
)

# 配当の基準日と corporate action がこの日数以内に並ぶ年度は換算しない。日本の分割は
# 「権利落ち = 基準日の前営業日、効力発生 = 基準日の翌日」が定型で、store が持つのは
# 権利落ち日だけなので、その配当が action の前の株数で払われたか後かを言えない。
DIVIDEND_RECORD_DATE_GUARD_DAYS = 5
# 総額から出した 1 株当たりと支払ごとの換算が食い違ってよい幅。総額は百万円単位で開示され、
# 割る株数は期末時点なので、支払の基準日の株数とは自社株買いのぶんだけずれる。
DIVIDEND_ROUTE_TOLERANCE = 0.05
# 期末発行済から自己株を引いた株数が、提出者自身が EPS を出すのに使った期中平均株数から
# この倍率を超えて外れる行は、per-share の分母に使わない。
SHARE_COUNT_ANCHOR_TOLERANCE = 2.0


def _shift_months(value: date, months: int) -> date:
    """`months` か月前後の同じ日。配当の基準日を四半期末に置くために使う。"""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(value.day, 28))


STALE_PRICE_MAX_LAG_DAYS = 15
BENCHMARK_TICKERS: tuple[str, ...] = (TOPIX_ETF_PROXY,)

# A window that ended in a completed cash tender offer is resolved by the price that
# offer paid, not by the last close before the target date. The status is separate from
# `resolved` so that a reader can always tell an observed market close from a settled
# takeover; the rules that produce these values are pre-registered in
# reports/studies/2026-08-11-capital-control-exit-values/.
CONTROL_EVENT_EXIT_STATUS = "resolved_control_event_exit"
RESOLVED_STATUSES = frozenset({"resolved", CONTROL_EVENT_EXIT_STATUS})


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardReturnRow:
    """One (asof, ticker, horizon) price observation and why it did not resolve.

    The row records what was observed; the coverage verdicts a cohort needs are
    derived from these observations at evaluation time so that every stored
    cohort is judged by the current contract rather than by whatever contract
    was in force when its cache was written.
    """

    asof: str
    ticker: str
    horizon: str
    target_date: str
    resolved: bool
    price_return: float | None
    stale_price: bool
    entry_date: str | None
    exit_date: str | None
    status: ForwardStatus = "resolved"
    adjustment_factor_coverage: AdjustmentCoverage = "unknown"
    realized_dividend_sum: float | None = None
    realized_dividend_fy_count: int = 0
    total_return: float | None = None
    total_return_status: TotalReturnStatus = "unresolved_price_return"
    total_return_basis: str = TOTAL_RETURN_BASIS


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlEventExit:
    """The cash a completed tender offer paid, and the day trading stopped."""

    delisted_on: date
    offer_price_yen: float


@dataclass(frozen=True, slots=True)
class _FYDividendObservation:
    fiscal_year_end: date
    disclosed_at: date
    dps_actual_annual: float | None
    # 会計期間と支払ごとの内訳。期間内に分割・併合が入った年度は、報告された年間値では
    # なくこちらを基準日ごとに換算する。総額と株数はその換算の独立した照合に使う。
    period_start: date | None = None
    payments: tuple[float | None, ...] = (None, None, None, None)
    dividend_total_annual: float | None = None
    shares_outstanding: float | None = None
    treasury_shares: float | None = None
    average_shares: float | None = None


FORWARD_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(ForwardReturnRow))


def read_control_event_exits(sqlite_path: Path) -> dict[str, tuple[ControlEventExit, ...]]:
    """Load realized tender-offer exits, keyed by ticker.

    A ticker can appear more than once — a name can be taken private, relist and be
    taken private again — so the caller matches the one that falls inside its window
    and leaves the row unresolved when more than one does.
    """
    # No fallback to an empty mapping. A store that cannot answer would produce a build
    # that is byte-identical to the baseline while presenting itself as the actual-exit
    # contract, and the before/after comparison would then read "nothing was replaced"
    # as a result rather than as a missing input.
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ticker, delisted_on, offer_price_yen FROM tender_offer_exit_values"
        ).fetchall()
    finally:
        conn.close()
    exits: dict[str, list[ControlEventExit]] = {}
    for ticker, delisted_on, price in rows:
        exits.setdefault(str(ticker), []).append(
            ControlEventExit(
                delisted_on=date.fromisoformat(str(delisted_on)), offer_price_yen=float(price)
            )
        )
    return {ticker: tuple(values) for ticker, values in exits.items()}


def compute_forward_returns(
    sqlite_path: Path,
    *,
    asofs: Sequence[date],
    tickers: Iterable[str],
    horizons: Sequence[str] = tuple(HORIZONS),
    control_event_exits: Mapping[str, Sequence[ControlEventExit]],
) -> list[ForwardReturnRow]:
    if not asofs:
        return []
    specs = tuple(require_horizon(name) for name in horizons)
    unique_tickers = sorted(set(tickers) | set(BENCHMARK_TICKERS))
    # Entry resolution accepts a bar up to STALE_PRICE_MAX_LAG_DAYS before asof, so
    # the load window has to start that far ahead of the earliest asof. Loading from
    # the asof itself makes the tolerance unusable: a name that did not trade on the
    # asof date reads as having no entry at all, even though it traded days earlier.
    min_asof = min(asofs) - timedelta(days=STALE_PRICE_MAX_LAG_DAYS)
    eval_cap = _latest_bar_date(sqlite_path)
    rows: list[ForwardReturnRow] = []
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        for ticker in unique_tickers:
            rows.extend(
                _ticker_forward_rows(
                    ticker,
                    _load_ticker_bars(conn, ticker, start=min_asof),
                    fy_dividends=_load_fy_dividends(conn, ticker, cutoff=eval_cap),
                    asofs=asofs,
                    horizons=specs,
                    eval_cap=eval_cap,
                    control_event_exits=tuple(control_event_exits.get(ticker, ())),
                )
            )
    finally:
        conn.close()
    return rows


def _ticker_forward_rows(
    ticker: str,
    bars: Sequence[JQuantsDailyBar],
    *,
    fy_dividends: Sequence[_FYDividendObservation] = (),
    asofs: Sequence[date],
    horizons: Sequence[HorizonSpec],
    eval_cap: date | None,
    control_event_exits: Sequence[ControlEventExit] = (),
) -> list[ForwardReturnRow]:
    dates = [bar.traded_at for bar in bars]
    closes = asof_basis_closes(bars) if bars else []
    rows: list[ForwardReturnRow] = []
    adjustment: AdjustmentCoverage
    if not bars or all(bar.adjustment_factor is None for bar in bars):
        adjustment = "unknown"
    elif all(bar.adjustment_factor is not None for bar in bars):
        adjustment = "complete"
    else:
        adjustment = "incomplete"
    specs = tuple(horizons)
    for asof in asofs:
        entry_index = _index_on_or_before(dates, asof)
        entry_date = dates[entry_index] if entry_index is not None else None
        entry_close = closes[entry_index] if entry_index is not None else None
        entry_valid = bool(
            entry_close
            and entry_close > 0
            and entry_date
            and (asof - entry_date).days <= STALE_PRICE_MAX_LAG_DAYS
        )
        for spec in specs:
            target = spec.target_date(asof)
            # One fully typed row per (asof, horizon), narrowed below. Spreading a dict
            # of the shared fields would erase the literal types every status relies on.
            base = ForwardReturnRow(
                asof=asof.isoformat(),
                ticker=ticker,
                horizon=spec.name,
                target_date=target.isoformat(),
                entry_date=entry_date.isoformat() if entry_date else None,
                resolved=False,
                price_return=None,
                stale_price=False,
                exit_date=None,
                status="unresolved_missing_entry",
                adjustment_factor_coverage=adjustment,
            )
            if not entry_valid:
                rows.append(base)
            elif eval_cap is None or target > eval_cap:
                rows.append(replace(base, status="unresolved_future_horizon"))
            else:
                exit_index = _index_on_or_before(dates, target)
                exit_date = dates[exit_index] if exit_index is not None else None
                exit_close = closes[exit_index] if exit_index is not None else None
                stale_exit = (
                    exit_date is not None and (target - exit_date).days > STALE_PRICE_MAX_LAG_DAYS
                )
                # Only a window the market could not close is eligible for the offer
                # price, and only when the offer itself resolves; anything else falls
                # through to the unresolved statuses the bracket already covers.
                control_row = (
                    _control_event_row(
                        base,
                        bars,
                        fy_dividends,
                        control_exit=_matching_control_event_exit(
                            control_event_exits, entry_date=entry_date, target=target
                        ),
                        entry_date=entry_date,
                        entry_close=float(entry_close or 0.0),
                        adjustment_coverage=adjustment,
                    )
                    if exit_close is None or stale_exit
                    else None
                )
                if control_row is not None:
                    rows.append(control_row)
                elif exit_close is None or exit_date is None:
                    rows.append(replace(base, status="unresolved_missing_exit"))
                elif stale_exit:
                    rows.append(
                        replace(
                            base,
                            stale_price=True,
                            exit_date=exit_date.isoformat(),
                            status="unresolved_stale_exit",
                        )
                    )
                else:
                    assert entry_date is not None
                    price_return = exit_close / float(entry_close or 0.0) - 1
                    dividend_sum, dividend_count, total_return, total_status = (
                        _resolve_total_return(
                            bars,
                            fy_dividends,
                            entry_date=entry_date,
                            exit_date=exit_date,
                            entry_close=float(entry_close or 0.0),
                            price_return=price_return,
                            adjustment_coverage=adjustment,
                        )
                    )
                    rows.append(
                        replace(
                            base,
                            resolved=True,
                            price_return=price_return,
                            exit_date=exit_date.isoformat(),
                            status="resolved",
                            realized_dividend_sum=dividend_sum,
                            realized_dividend_fy_count=dividend_count,
                            total_return=total_return,
                            total_return_status=total_status,
                        )
                    )
    return rows


def _matching_control_event_exit(
    exits: Sequence[ControlEventExit], *, entry_date: date | None, target: date
) -> ControlEventExit | None:
    """The one settled offer that ended trading inside this window, if exactly one did."""
    if entry_date is None:
        return None
    matching = [
        control_exit for control_exit in exits if entry_date < control_exit.delisted_on <= target
    ]
    return matching[0] if len(matching) == 1 else None


def _control_event_row(
    base: ForwardReturnRow,
    bars: Sequence[JQuantsDailyBar],
    fy_dividends: Sequence[_FYDividendObservation],
    *,
    control_exit: ControlEventExit | None,
    entry_date: date | None,
    entry_close: float,
    adjustment_coverage: AdjustmentCoverage,
) -> ForwardReturnRow | None:
    """Price the window with the offer, or decline and leave it to the bracket.

    The offer is quoted per share as of the delisting, while entry closes are carried to
    the share basis of the final bar. Those two bases agree only when no split or reverse
    split followed the delisting, so a non-unit cumulative factor after that day means
    the offer cannot be placed on the same basis and the window is not realized.
    """
    if control_exit is None or entry_date is None or entry_close <= 0 or not bars:
        return None
    if adjustment_coverage != "complete":
        return None
    factor = _cumulative_adjustment_factor_after(
        bars, after=control_exit.delisted_on, asof_date=bars[-1].traded_at
    )
    if factor != 1.0:
        return None
    price_return = control_exit.offer_price_yen / entry_close - 1
    if not isfinite(price_return) or price_return < -1:
        return None
    dividend_sum, dividend_count, total_return, total_status = _resolve_total_return(
        bars,
        fy_dividends,
        entry_date=entry_date,
        exit_date=control_exit.delisted_on,
        entry_close=entry_close,
        price_return=price_return,
        adjustment_coverage=adjustment_coverage,
    )
    return replace(
        base,
        resolved=True,
        price_return=price_return,
        exit_date=control_exit.delisted_on.isoformat(),
        status=CONTROL_EVENT_EXIT_STATUS,
        realized_dividend_sum=dividend_sum,
        realized_dividend_fy_count=dividend_count,
        total_return=total_return,
        total_return_status=total_status,
    )


def _index_on_or_before(dates: Sequence[date], target: date) -> int | None:
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo else None


def _load_ticker_bars(
    conn: sqlite3.Connection, ticker: str, *, start: date
) -> list[JQuantsDailyBar]:
    rows = conn.execute(
        "SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at >= ? AND close IS NOT NULL ORDER BY traded_at",
        (ticker, start.isoformat()),
    ).fetchall()
    return [
        JQuantsDailyBar(
            ticker=ticker,
            traded_at=date.fromisoformat(str(day)),
            close=float(close),
            turnover_value=None,
            adjustment_factor=float(factor) if factor is not None else None,
        )
        for day, close, factor in rows
    ]


def _load_fy_dividends(
    conn: sqlite3.Connection, ticker: str, *, cutoff: date | None
) -> list[_FYDividendObservation]:
    if cutoff is None:
        return []
    rows = conn.execute(
        "SELECT fiscal_year_end, disclosed_at, period_start, dps_actual_annual, "
        "dividend_q1, dividend_interim, dividend_q3, dividend_year_end, "
        "dividend_total_annual, shares_outstanding, treasury_shares, average_shares "
        "FROM jquants_fin_summaries "
        "WHERE ticker = ? AND fiscal_period = 'FY' AND fiscal_year_end IS NOT NULL "
        "AND disclosed_at <= ? ORDER BY fiscal_year_end, disclosed_at",
        (ticker, cutoff.isoformat()),
    ).fetchall()
    return [
        _FYDividendObservation(
            fiscal_year_end=date.fromisoformat(str(row[0])),
            disclosed_at=date.fromisoformat(str(row[1])),
            dps_actual_annual=(float(row[3]) if row[3] is not None else None),
            period_start=(date.fromisoformat(str(row[2])) if row[2] is not None else None),
            payments=tuple(float(value) if value is not None else None for value in row[4:8]),
            dividend_total_annual=(float(row[8]) if row[8] is not None else None),
            shares_outstanding=(float(row[9]) if row[9] is not None else None),
            treasury_shares=(float(row[10]) if row[10] is not None else None),
            average_shares=(float(row[11]) if row[11] is not None else None),
        )
        for row in rows
    ]


def _resolve_total_return(
    bars: Sequence[JQuantsDailyBar],
    observations: Sequence[_FYDividendObservation],
    *,
    entry_date: date,
    exit_date: date,
    entry_close: float,
    price_return: float,
    adjustment_coverage: AdjustmentCoverage,
) -> tuple[float | None, int, float | None, TotalReturnStatus]:
    """Resolve retrospective FY dividends without treating absent data as zero."""
    if adjustment_coverage != "complete":
        return None, 0, None, "unresolved_adjustment_factor"
    by_fiscal_year_end: dict[date, list[_FYDividendObservation]] = {}
    for observation in observations:
        if entry_date < observation.fiscal_year_end <= exit_date:
            by_fiscal_year_end.setdefault(observation.fiscal_year_end, []).append(observation)
    if not by_fiscal_year_end:
        return None, 0, None, "unresolved_no_fy_observation"

    selected: list[_FYDividendObservation] = []
    for fiscal_year_end in sorted(by_fiscal_year_end):
        disclosed = [
            observation
            for observation in by_fiscal_year_end[fiscal_year_end]
            if observation.dps_actual_annual is not None
        ]
        if not disclosed:
            return None, 0, None, "unresolved_missing_dividend"
        latest = max(disclosed, key=lambda observation: observation.disclosed_at)
        assert latest.dps_actual_annual is not None
        if not isfinite(latest.dps_actual_annual) or latest.dps_actual_annual < 0:
            return None, 0, None, "unresolved_invalid_dividend"
        selected.append(latest)

    basis_date = bars[-1].traded_at
    dividend_sum = 0.0
    for observation in selected:
        resolved = _asof_basis_dividend(observation, bars, basis_date=basis_date)
        if resolved is None:
            return None, 0, None, "unresolved_dividend_split_basis"
        dividend_sum += resolved
    if not isfinite(dividend_sum) or entry_close <= 0:
        return None, 0, None, "unresolved_invalid_dividend"
    total_return = price_return + dividend_sum / entry_close
    if not isfinite(total_return) or total_return < -1:
        return None, 0, None, "unresolved_invalid_total_return"
    return dividend_sum, len(selected), total_return, "resolved"


def _asof_basis_dividend(
    observation: _FYDividendObservation,
    bars: Sequence[JQuantsDailyBar],
    *,
    basis_date: date,
) -> float | None:
    """その年度の年間配当を basis_date の株式基準で答える。答えられなければ None。

    報告される年間 DPS は中間・期末それぞれの基準日時点の株式基準なので、会計期間に
    分割・併合が入ると開示日を基準にした単一の factor では換算できない。期間内に何も
    起きていなければ開示日より後の調整だけで足り、起きていれば支払ごとにその基準日より
    後の調整を掛ける。screening の carry と同じ規約で、較正した量とランキングへ入れる量
    の定義を揃える。
    """
    reported = observation.dps_actual_annual
    if reported is None:
        return None
    window_start = observation.period_start or _shift_months(observation.fiscal_year_end, -12)
    if (
        _cumulative_adjustment_factor_after(
            bars, after=window_start, asof_date=observation.disclosed_at
        )
        == 1.0
    ):
        return reported * _cumulative_adjustment_factor_after(
            bars, after=observation.disclosed_at, asof_date=basis_date
        )

    payments = tuple(
        zip(
            observation.payments,
            (
                _shift_months(observation.fiscal_year_end, -9),
                _shift_months(observation.fiscal_year_end, -6),
                _shift_months(observation.fiscal_year_end, -3),
                observation.fiscal_year_end,
            ),
            strict=True,
        )
    )
    if all(value is None for value, _ in payments):
        return None
    guard = timedelta(days=DIVIDEND_RECORD_DATE_GUARD_DAYS)
    adjustments = [
        bar.traded_at
        for bar in bars
        if window_start < bar.traded_at <= observation.disclosed_at
        and bar.adjustment_factor not in (None, 0.0, 1.0)
    ]
    for value, record_date in payments:
        if value and any(abs(day - record_date) <= guard for day in adjustments):
            return None
    resolved = sum(
        (value or 0.0)
        * _cumulative_adjustment_factor_after(bars, after=record_date, asof_date=basis_date)
        for value, record_date in payments
    )
    if resolved <= 0:
        return None
    shares = _per_share_denominator(observation)
    amount = observation.dividend_total_annual
    if (
        amount is not None
        and amount > 0
        and shares is not None
        and abs((amount / shares) / resolved - 1.0) > DIVIDEND_ROUTE_TOLERANCE
    ):
        return None
    return resolved


def _per_share_denominator(observation: _FYDividendObservation) -> float | None:
    """円の総額を 1 株当たりへ直せる株数。提出者自身の期中平均から外れる行は答えない。"""
    shares = observation.shares_outstanding
    treasury = observation.treasury_shares
    if shares is None or treasury is None:
        return None
    remaining = shares - treasury
    if remaining <= 0:
        return None
    anchor = observation.average_shares
    if anchor is None or anchor <= 0:
        return remaining
    ratio = remaining / anchor
    if not 1 / SHARE_COUNT_ANCHOR_TOLERANCE <= ratio <= SHARE_COUNT_ANCHOR_TOLERANCE:
        return None
    return remaining


def _cumulative_adjustment_factor_after(
    bars: Sequence[JQuantsDailyBar], *, after: date, asof_date: date
) -> float:
    """Match per-share facts to the final share basis used by asof_basis_closes."""
    factor = 1.0
    for bar in bars:
        if bar.traded_at <= after or bar.traded_at > asof_date:
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        factor *= bar.adjustment_factor
    return factor


def _latest_bar_date(sqlite_path: Path) -> date | None:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    return date.fromisoformat(str(row[0])) if row and row[0] is not None else None
