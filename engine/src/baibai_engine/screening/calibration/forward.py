"""Calendar-aware, fail-visible price and total-return forward observations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.price_basis import asof_basis_closes, asof_basis_prices
from baibai_engine.storage.observations.reader import read_financial_documents

# 配当・株数の基準はValuationのcarryと同じにする。較正と本番が異なる量を扱わないよう、
# 規約の定数は計算を所有するValuationから読み、ここへ重複定義しない。
from baibai_engine.valuation.metrics.dividends import (
    _dividend_basis_requires_resolution,
)
from baibai_engine.valuation.metrics.periods import _latest_consistent_actual, _period_days

from .horizons import HORIZONS, STALE_PRICE_MAX_LAG_DAYS, HorizonSpec, require_horizon

__all__ = (
    "DEFAULT_FORWARD_OBSERVATION_POLICY",
    "HORIZONS",
    "ControlEventExit",
    "FailureExit",
    "ForwardObservationPolicy",
    "ForwardReturnRow",
    "compute_forward_returns",
    "is_failure_delisting",
    "read_control_event_exits",
    "read_failure_exits",
)


FORWARD_CALCULATION_REVISION = "reported-dividend-period-gaps-v7"


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardObservationPolicy:
    """The observation rules that decide a forward row's status, exit, and return.

    A forward cohort measured under different rules is a different measurement, not a
    rebuild of the same one. The policy is folded into the forward build identity, so
    a store cannot hold one range observed with control-event exits and another range
    observed without them: carrying a partition built under other rules is refused
    rather than silently republished under this build's fingerprint.
    """

    use_control_event_exits: bool = True
    use_failure_exits: bool = True

    @property
    def digest(self) -> str:
        """A canonical statement of the policy, stable across field additions."""
        policy = "|".join(f"{field.name}={getattr(self, field.name)!r}" for field in fields(self))
        return f"{FORWARD_CALCULATION_REVISION}|{policy}"


DEFAULT_FORWARD_OBSERVATION_POLICY = ForwardObservationPolicy()

ForwardStatus = str
AdjustmentCoverage = str
TotalReturnStatus = str
TOTAL_RETURN_BASIS = "fy_actual_dividend_fiscal_year_end_window"
TOTAL_RETURN_STATUSES = frozenset(
    {
        "resolved",
        "unresolved_price_return",
        "unresolved_adjustment_factor",
        "unresolved_rights_issue",
        "unresolved_no_fy_observation",
        "unresolved_missing_dividend",
        "unresolved_dividend_period_gap",
        "unresolved_invalid_dividend",
        "unresolved_invalid_total_return",
        # 会計期間から開示までに分割・併合が入り、原本の株式基準を特定できない年度。
        # 総リターンは配当を含む定義なので、基準の分からない配当を足した値を resolved と
        # 名乗らせない。この窓は総リターンを読む metric の標本から外れる。
        "unresolved_dividend_split_basis",
    }
)


BENCHMARK_TICKERS: tuple[str, ...] = (TOPIX_ETF_PROXY,)

# A delisted window may use the verified cash offer price as an analytical exit proxy.
# Keep a distinct status: this is neither a target-date market close nor proof of an
# individual shareholder's cash receipt. Historical rules and measurements are in
# reports/studies/2026-08-11-capital-control-exit-values/.
CONTROL_EVENT_EXIT_STATUS = "resolved_control_event_exit"

# A qualifying failure delisting uses the last traded close as its exit observation.
# This is not proof of a final cash recovery or a worthless unlisted holding. Keep the
# status separate so readers can distinguish this convention from a target-date close.
FAILURE_EXIT_STATUS = "resolved_failure_exit"
RESOLVED_STATUSES = frozenset({"resolved", CONTROL_EVENT_EXIT_STATUS, FAILURE_EXIT_STATUS})

# JPX writes the delisting reason as free prose, so the classification is by the phrases
# the exchange actually uses. It is deliberately fail-closed: a reason that carries no
# failure phrase, or that carries one alongside an acquisition phrase, is left to the
# unresolved statuses rather than priced at a last close. Getting an acquisition wrong
# would book a takeover premium as a wipeout; leaving a failure unclassified only
# preserves the omission that already exists.
_FAILURE_REASON_PHRASES = (
    "破産",
    "民事再生",
    "会社更生",
    "債務超過",
    "上場維持基準",
    "内部管理体制",
    "虚偽記載",
    "提出遅延",
    "提出の遅延",
    "時価総額が所要額未満",
    "業績基準",
    "宣誓書における重大な違反",
)
# `株式の併合` is not here: on its own it is the second step of a squeeze-out, which is
# an acquisition rather than a failure. It only ever appears in a failure reason next to
# an explicit insolvency phrase, and that reason is classified by the insolvency.
_ACQUISITION_REASON_PHRASES = (
    "完全子会社化",
    "買収",
    "公開買付",
    "株式等売渡請求",
    "合併",
    "ＭＢＯ",
    "MBO",
    "株式移転",
    "株式交換",
)


def is_failure_delisting(reason: str) -> bool:
    """Whether the reason qualifies for the last-traded-close exit convention."""
    if any(phrase in reason for phrase in _ACQUISITION_REASON_PHRASES):
        return False
    return any(phrase in reason for phrase in _FAILURE_REASON_PHRASES)


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
    """The offer-price proxy and delisting date, not confirmed payment facts."""

    delisted_on: date
    offer_price_yen: float


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureExit:
    """The day the exchange removed a failing company, and the reason it gave."""

    delisted_on: date
    reason: str


@dataclass(frozen=True, slots=True)
class _FYDividendObservation:
    fiscal_year_end: date
    disclosed_at: date
    dps_actual_annual: float | None
    period_start: date | None = None


FORWARD_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(ForwardReturnRow))


def read_control_event_exits(sqlite_path: Path) -> dict[str, tuple[ControlEventExit, ...]]:
    """Load tender-offer-price exit proxies, keyed by ticker.

    A ticker can appear more than once — a name can be taken private, relist and be
    taken private again — so the caller matches the one that falls inside its window
    and leaves the row unresolved when more than one does.
    """
    # No fallback to an empty mapping. A store that cannot answer would produce a build
    # that is byte-identical to the baseline while presenting itself as the offer-price exit
    # contract, and the before/after comparison would then read "nothing was replaced"
    # as a result rather than as a missing input.
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
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


def read_failure_exits(sqlite_path: Path) -> dict[str, tuple[FailureExit, ...]]:
    """Load the delistings that removed a failing company, keyed by ticker.

    The reason is carried through so a row can show its classification, and so a
    later reading of the same store can tell a reclassification from a data change.
    """
    # No fallback to an empty mapping, for the same reason `read_control_event_exits`
    # has none: a store that cannot answer would build a byte-identical baseline while
    # presenting itself as the delisting-exit contract.
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT ticker, delisted_on, reason FROM jpx_delistings").fetchall()
    finally:
        conn.close()
    exits: dict[str, list[FailureExit]] = {}
    for ticker, delisted_on, reason in rows:
        if not is_failure_delisting(str(reason)):
            continue
        exits.setdefault(str(ticker), []).append(
            FailureExit(delisted_on=date.fromisoformat(str(delisted_on)), reason=str(reason))
        )
    return {ticker: tuple(values) for ticker, values in exits.items()}


def compute_forward_returns(
    sqlite_path: Path,
    *,
    asofs: Sequence[date],
    tickers: Iterable[str],
    horizons: Sequence[str] = tuple(HORIZONS),
    control_event_exits: Mapping[str, Sequence[ControlEventExit]],
    failure_exits: Mapping[str, Sequence[FailureExit]],
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
    latest_target = max(spec.target_date(asof) for asof in asofs for spec in specs)
    eval_cap = latest_market_data_date(sqlite_path)
    rows: list[ForwardReturnRow] = []
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for ticker in unique_tickers:
            bars = _load_ticker_bars(conn, ticker, start=min_asof)
            fy_dividends = _load_fy_dividends(conn, ticker, cutoff=eval_cap)
            # Price entry needs only the stale-price window. The first FY dividend
            # selected after entry can belong to a fiscal year that began almost a
            # year earlier, so its factor events need a separate, earlier boundary.
            dividend_period_starts = [
                observation.period_start
                for observation in fy_dividends
                if min_asof < observation.fiscal_year_end <= latest_target
                and observation.period_start is not None
            ]
            event_start = min([min_asof, *dividend_period_starts])
            adjustment_events = _load_ticker_adjustment_events(conn, ticker, start=event_start)
            rows.extend(
                _ticker_forward_rows(
                    ticker,
                    bars,
                    adjustment_events=adjustment_events,
                    fy_dividends=fy_dividends,
                    asofs=asofs,
                    horizons=specs,
                    eval_cap=eval_cap,
                    control_event_exits=tuple(control_event_exits.get(ticker, ())),
                    failure_exits=tuple(failure_exits.get(ticker, ())),
                )
            )
    finally:
        conn.close()
    return rows


def _ticker_forward_rows(
    ticker: str,
    bars: Sequence[JQuantsDailyBar],
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
    fy_dividends: Sequence[_FYDividendObservation] = (),
    asofs: Sequence[date],
    horizons: Sequence[HorizonSpec],
    eval_cap: date | None,
    control_event_exits: Sequence[ControlEventExit] = (),
    failure_exits: Sequence[FailureExit] = (),
) -> list[ForwardReturnRow]:
    dates = [bar.traded_at for bar in bars]
    events = bars if adjustment_events is None else adjustment_events
    price_basis_date = eval_cap or (bars[-1].traded_at if bars else None)
    closes = asof_basis_closes(bars, events, asof_date=price_basis_date) if bars else []
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
                        adjustment_events=events,
                        price_basis_date=price_basis_date,
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
                # A delisting for failure is priced only where the market itself could
                # not close the window, so this is tried after the offer price and after
                # the ordinary close: a window whose target date has a fresh bar is
                # already resolved by that bar and is not revisited here.
                failure_row = (
                    _failure_exit_row(
                        base,
                        bars,
                        fy_dividends,
                        adjustment_events=events,
                        price_basis_date=price_basis_date,
                        failure_exit=_matching_failure_exit(
                            failure_exits, entry_date=entry_date, target=target
                        ),
                        entry_date=entry_date,
                        entry_close=float(entry_close or 0.0),
                        exit_date=exit_date,
                        exit_close=exit_close,
                        adjustment_coverage=adjustment,
                    )
                    if control_row is None and stale_exit
                    else None
                )
                if control_row is not None:
                    rows.append(control_row)
                elif failure_row is not None:
                    rows.append(failure_row)
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
                            adjustment_events=events,
                            price_basis_date=price_basis_date,
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
    """The one offer-price proxy whose delisting falls inside this window."""
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
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    price_basis_date: date | None,
    control_exit: ControlEventExit | None,
    entry_date: date | None,
    entry_close: float,
    adjustment_coverage: AdjustmentCoverage,
) -> ForwardReturnRow | None:
    """Price the window with the offer, or decline and leave it to the bracket.

    This convention places the verified offer price at delisting; it does not prove
    a holder received that amount on that date. Entry closes use the final price basis,
    so a non-unit cumulative factor after delisting leaves the proxy incomparable.
    The stored offer evidence does not establish a later share-unit conversion.
    """
    if control_exit is None or entry_date is None or entry_close <= 0 or not bars:
        return None
    if adjustment_coverage != "complete":
        return None
    if price_basis_date is None:
        return None
    factor = asof_basis_prices(
        [(control_exit.delisted_on, 1.0)],
        [(event.traded_at, event.adjustment_factor) for event in adjustment_events],
        asof_date=price_basis_date,
    )[0]
    if factor != 1.0:
        return None
    price_return = control_exit.offer_price_yen / entry_close - 1
    if not isfinite(price_return) or price_return < -1:
        return None
    dividend_sum, dividend_count, total_return, total_status = _resolve_total_return(
        bars,
        fy_dividends,
        adjustment_events=adjustment_events,
        price_basis_date=price_basis_date,
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


def _matching_failure_exit(
    exits: Sequence[FailureExit], *, entry_date: date | None, target: date
) -> FailureExit | None:
    """The one failure delisting that ended trading inside this window, if exactly one did."""
    if entry_date is None:
        return None
    matching = [
        failure_exit for failure_exit in exits if entry_date < failure_exit.delisted_on <= target
    ]
    return matching[0] if len(matching) == 1 else None


def _failure_exit_row(
    base: ForwardReturnRow,
    bars: Sequence[JQuantsDailyBar],
    fy_dividends: Sequence[_FYDividendObservation],
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    price_basis_date: date | None,
    failure_exit: FailureExit | None,
    entry_date: date | None,
    entry_close: float,
    exit_date: date | None,
    exit_close: float | None,
    adjustment_coverage: AdjustmentCoverage,
) -> ForwardReturnRow | None:
    """Price the window with the last close before the delisting, or decline.

    Entry and exit closes both come from the same adjusted series, so no basis
    reconciliation is needed here — unlike an offer price, which is quoted on the share
    basis of the delisting day. Adjustment coverage is therefore recorded on the row and
    not gated on, exactly as it is for a window the market closed itself: this is the same
    arithmetic as `resolved`, taken at a date the target could not reach.

    The exit has to fall on or before the delisting, which is what distinguishes a company
    the exchange removed from one that merely stopped trading: a name that traded again
    afterwards is left unresolved rather than priced at a close that was not its last.

    A long suspension can leave this close far from final recoverable value. The row
    records a market-price convention, not a cash settlement; the bias direction and
    size are not established by the delisting reason alone.
    """
    if failure_exit is None or entry_date is None or entry_close <= 0:
        return None
    if exit_date is None or exit_close is None or exit_close <= 0 or not bars:
        return None
    if exit_date > failure_exit.delisted_on:
        return None
    price_return = exit_close / entry_close - 1
    if not isfinite(price_return) or price_return < -1:
        return None
    dividend_sum, dividend_count, total_return, total_status = _resolve_total_return(
        bars,
        fy_dividends,
        adjustment_events=adjustment_events,
        price_basis_date=price_basis_date,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_close=entry_close,
        price_return=price_return,
        adjustment_coverage=adjustment_coverage,
    )
    # `stale_price` stays false, as it does for an offer price. It marks a price the
    # tolerance rejected, and this exit convention accepts the last close; `exit_date`
    # says exactly when the window ended, and `failure_exit_count` is where these are
    # counted.
    return replace(
        base,
        resolved=True,
        price_return=price_return,
        exit_date=exit_date.isoformat(),
        status=FAILURE_EXIT_STATUS,
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
        "SELECT traded_at, close, adjustment_factor, ex_right_type FROM jquants_daily_bars "
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
            ex_right_type=ex_right_type,
        )
        for day, close, factor, ex_right_type in rows
    ]


def _load_ticker_adjustment_events(
    conn: sqlite3.Connection, ticker: str, *, start: date
) -> list[JQuantsAdjustmentFactorEvent]:
    rows = conn.execute(
        "SELECT traded_at, adjustment_factor, ex_right_type FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at >= ? "
        "AND (adjustment_factor != 1.0 OR ex_right_type IS NOT NULL) ORDER BY traded_at",
        (ticker, start.isoformat()),
    ).fetchall()
    return [
        JQuantsAdjustmentFactorEvent(
            ticker=ticker,
            traded_at=date.fromisoformat(str(day)),
            adjustment_factor=float(factor),
            ex_right_type=ex_right_type,
        )
        for day, factor, ex_right_type in rows
    ]


def _load_fy_dividends(
    conn: sqlite3.Connection, ticker: str, *, cutoff: date | None
) -> list[_FYDividendObservation]:
    if cutoff is None:
        return []
    by_year: dict[date, list[JQuantsFinancialSummary]] = {}
    for row in read_financial_documents(conn, start=date.min, end=cutoff, ticker=ticker):
        if row.fiscal_period == "FY" and row.fiscal_year_end is not None:
            by_year.setdefault(row.fiscal_year_end, []).append(row)
    observations = []
    for year, rows in sorted(by_year.items()):
        # A forecast-only revision does not cancel a recorded actual dividend.
        # Same-day revisions use the shared disclosure-time/conflict contract.
        candidates = [row for row in rows if row.dps_actual_annual is not None]
        selected = _latest_consistent_actual(candidates, ("dps_actual_annual",))
        if selected is None:
            # Retain the fiscal year as unresolved, rather than dropping its dividend.
            observations.append(
                _FYDividendObservation(year, max(row.disclosed_at for row in rows), None)
            )
            continue
        observations.append(
            _FYDividendObservation(
                fiscal_year_end=year,
                disclosed_at=selected.disclosed_at,
                dps_actual_annual=selected.dps_actual_annual
                if selected.dps_actual_annual == 0 or _period_days(selected) is not None
                else None,
                period_start=selected.period_start,
            )
        )
    return observations


def _resolve_total_return(
    bars: Sequence[JQuantsDailyBar],
    observations: Sequence[_FYDividendObservation],
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
    price_basis_date: date | None = None,
    entry_date: date,
    exit_date: date,
    entry_close: float,
    price_return: float,
    adjustment_coverage: AdjustmentCoverage,
) -> tuple[float | None, int, float | None, TotalReturnStatus]:
    """Resolve retrospective FY dividends without treating absent data as zero."""
    if adjustment_coverage != "complete":
        return None, 0, None, "unresolved_adjustment_factor"
    events = bars if adjustment_events is None else adjustment_events
    if any(
        entry_date < event.traded_at <= exit_date and event.ex_right_type == "3" for event in events
    ):
        # Price adjustment alone does not reveal proceeds from selling rights or
        # the capital paid to exercise them. Do not call that a realized total return.
        return None, 0, None, "unresolved_rights_issue"
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

    # Missing annual documents are not zero-dividend observations. A known
    # period starting after entry, or a break between selected fiscal periods,
    # proves that this sum cannot represent all selected FY dividends. Use actual
    # period boundaries rather than assuming every issuer has twelve-month years.
    previous_end = entry_date - timedelta(days=1)
    for observation in selected:
        if (
            observation.period_start is not None
            and observation.period_start > previous_end + timedelta(days=1)
        ):
            return None, 0, None, "unresolved_dividend_period_gap"
        previous_end = observation.fiscal_year_end

    basis_date = price_basis_date or bars[-1].traded_at
    dividend_sum = 0.0
    for observation in selected:
        resolved = _asof_basis_dividend(observation, events, basis_date=basis_date)
        if resolved is None:
            return None, 0, None, "unresolved_dividend_split_basis"
        dividend_sum += resolved
    # A later rights event rescales both prices of an already completed window.
    # Express its cash component on that same comparison basis, without pretending
    # the event changed share units or occurred during the holding period.
    dividend_sum *= asof_basis_prices(
        [(exit_date, 1.0)],
        [
            (event.traded_at, event.adjustment_factor)
            for event in events
            if event.ex_right_type == "3"
        ],
        asof_date=basis_date,
    )[0]
    if not isfinite(dividend_sum) or entry_close <= 0:
        return None, 0, None, "unresolved_invalid_dividend"
    total_return = price_return + dividend_sum / entry_close
    if not isfinite(total_return) or total_return < -1:
        return None, 0, None, "unresolved_invalid_total_return"
    return dividend_sum, len(selected), total_return, "resolved"


def _asof_basis_dividend(
    observation: _FYDividendObservation,
    bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    basis_date: date,
) -> float | None:
    """Use reported DPS only when observed actions do not make its basis ambiguous.

    No split factor reconstructs issuer payment basis, including actions after
    disclosure. Absence of an observed action is not a guarantee that an issuer
    did not pre-adjust for a future announced action. Keep price-only outcomes.
    """
    reported = observation.dps_actual_annual
    if reported is None or reported == 0:
        return reported
    window_start = observation.period_start
    if (
        window_start is None
        or window_start > observation.fiscal_year_end
        or observation.fiscal_year_end > observation.disclosed_at
    ):
        return None
    if _dividend_basis_requires_resolution(bars, accrual_start=window_start, basis_date=basis_date):
        return None
    return reported


def latest_market_data_date(sqlite_path: Path) -> date | None:
    """Return the observation cutoff used by forward-return computation."""

    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    return date.fromisoformat(str(row[0])) if row and row[0] is not None else None
