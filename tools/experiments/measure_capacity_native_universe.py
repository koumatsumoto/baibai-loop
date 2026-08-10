"""Outcome-free feasibility measurement for issue #884.

This stage reads only point-in-time panel inputs and market facts dated on or
before each cohort as-of.  It intentionally has no forward-return reader.  The
result freezes whether the capacity comparison is executable before a later
commit is allowed to inspect outcomes.
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import TextIO

import yaml

from baibai_engine.screening.calibration.horizons import require_horizon

DEFAULT_CALIBRATION_DIR = Path("stores/screening/calibration")
DEFAULT_MARKET_DB = Path("stores/market/market.sqlite")
TRADING_UNIT = 100
SESSION_COUNT = 60
MIN_USABLE_SESSIONS = 57
MIN_NONZERO_VOLUME_SHARE = 0.95
MAX_CAPACITY_DAYS = 5.0
PARTICIPATION_RATE = 0.01
MIN_CAPACITY_LISTING_SPAN_DAYS = 365
CURRENT_MIN_MARKET_CAP_OKU = 100.0
CURRENT_MIN_AVG_TURNOVER_OKU = 1.0
CURRENT_MIN_LISTING_SPAN_DAYS = 182
NOTIONALS = {"starter": 100_000.0, "standard": 300_000.0, "stress": 500_000.0}
PRIMARY_NOTIONAL = "standard"
MIN_FACT_COVERAGE = 0.95
MIN_TOP20_AVAILABILITY = 0.90
MIN_INCREMENTAL_TOP5_AVAILABILITY = 0.75
MAX_TICKER_SHARE = 0.15
JPX_TRADING_UNIT_SOURCE = "https://www.jpx.co.jp/equities/improvements/unit/index.html"


class CapacityStudyError(ValueError):
    """Raised when Stage 0 cannot prove its point-in-time input contract."""


@dataclass(frozen=True, slots=True)
class MarketBar:
    traded_at: date
    close: float | None
    adjusted_close: float | None
    volume: float | None
    turnover_value: float | None


@dataclass(frozen=True, slots=True)
class CapacityFact:
    minimum_lot_yen: float | None
    trading_value_median_60d: float
    trading_value_p20_60d: float
    usable_sessions: int
    expected_sessions: int
    nonzero_volume_share_60d: float
    no_trade_share_60d: float
    zero_return_share_60d: float | None
    return_pair_count: int
    amihud_illiquidity_median_60d: float | None
    amihud_illiquidity_p90_60d: float | None
    capacity_days_by_notional: Mapping[str, float | None]
    core_fact_available: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    sector: str
    market: str | None
    er_annual: float
    er_carry_annual: float | None
    er_reversion_annual: float | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    listing_span_days: int | None
    fact: CapacityFact


def _optional_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: object) -> int | None:
    parsed = _optional_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _is_true(value: object) -> bool:
    return str(value).lower() in {"true", "1"}


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Conservative empirical percentile with no interpolation."""

    if not values:
        raise CapacityStudyError("percentile requires at least one observation")
    if not 0 <= percentile <= 1:
        raise CapacityStudyError("percentile must be between zero and one")
    ordered = sorted(values)
    rank = max(math.ceil(percentile * len(ordered)), 1)
    return ordered[rank - 1]


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return {"n": 0, "p10": None, "p25": None, "median": None, "p75": None, "p90": None}
    return {
        "n": len(finite),
        "p10": _nearest_rank_percentile(finite, 0.10),
        "p25": _nearest_rank_percentile(finite, 0.25),
        "median": median(finite),
        "p75": _nearest_rank_percentile(finite, 0.75),
        "p90": _nearest_rank_percentile(finite, 0.90),
    }


def _capacity_fact(
    *,
    session_dates: Sequence[date],
    bars: Mapping[date, MarketBar],
) -> CapacityFact:
    if len(session_dates) != SESSION_COUNT:
        raise CapacityStudyError(f"capacity fact requires exactly {SESSION_COUNT} market sessions")
    turnovers: list[float] = []
    usable = 0
    nonzero = 0
    for session in session_dates:
        bar = bars.get(session)
        turnover = bar.turnover_value if bar is not None else None
        volume = bar.volume if bar is not None else None
        close = bar.close if bar is not None else None
        adjusted = bar.adjusted_close if bar is not None else None
        turnovers.append(turnover if turnover is not None and turnover >= 0 else 0.0)
        valid = (
            close is not None
            and close > 0
            and adjusted is not None
            and adjusted > 0
            and volume is not None
            and volume >= 0
            and turnover is not None
            and turnover >= 0
        )
        usable += valid
        if valid:
            assert volume is not None
            assert turnover is not None
            nonzero += volume > 0 and turnover > 0

    asof_bar = bars.get(session_dates[-1])
    minimum_lot = (
        asof_bar.close * TRADING_UNIT
        if asof_bar is not None and asof_bar.close is not None and asof_bar.close > 0
        else None
    )
    p20 = _nearest_rank_percentile(turnovers, 0.20)
    capacity_days = {
        name: (
            max(notional, minimum_lot) / (p20 * PARTICIPATION_RATE)
            if minimum_lot is not None and p20 > 0
            else None
        )
        for name, notional in NOTIONALS.items()
    }

    returns: list[float] = []
    amihud: list[float] = []
    for prior_date, current_date in pairwise(session_dates):
        prior = bars.get(prior_date)
        current = bars.get(current_date)
        if (
            prior is None
            or current is None
            or prior.adjusted_close is None
            or prior.adjusted_close <= 0
            or current.adjusted_close is None
            or current.adjusted_close <= 0
        ):
            continue
        daily_return = current.adjusted_close / prior.adjusted_close - 1.0
        returns.append(daily_return)
        if current.turnover_value is not None and current.turnover_value > 0:
            amihud.append(abs(daily_return) / current.turnover_value)

    return CapacityFact(
        minimum_lot_yen=minimum_lot,
        trading_value_median_60d=median(turnovers),
        trading_value_p20_60d=p20,
        usable_sessions=usable,
        expected_sessions=SESSION_COUNT,
        nonzero_volume_share_60d=nonzero / SESSION_COUNT,
        no_trade_share_60d=1.0 - nonzero / SESSION_COUNT,
        zero_return_share_60d=(
            sum(value == 0 for value in returns) / len(returns) if returns else None
        ),
        return_pair_count=len(returns),
        amihud_illiquidity_median_60d=(median(amihud) if amihud else None),
        amihud_illiquidity_p90_60d=(_nearest_rank_percentile(amihud, 0.90) if amihud else None),
        capacity_days_by_notional=capacity_days,
        core_fact_available=minimum_lot is not None,
    )


def _current_eligible(candidate: Candidate) -> bool:
    return (
        candidate.market_cap_oku is not None
        and candidate.market_cap_oku >= CURRENT_MIN_MARKET_CAP_OKU
        and candidate.avg_turnover_oku is not None
        and candidate.avg_turnover_oku >= CURRENT_MIN_AVG_TURNOVER_OKU
        and candidate.listing_span_days is not None
        and candidate.listing_span_days >= CURRENT_MIN_LISTING_SPAN_DAYS
    )


def _capacity_eligible(candidate: Candidate, notional_name: str) -> bool:
    days = candidate.fact.capacity_days_by_notional[notional_name]
    notional = NOTIONALS[notional_name]
    return (
        candidate.fact.core_fact_available
        and candidate.fact.minimum_lot_yen is not None
        and candidate.fact.minimum_lot_yen <= notional
        and candidate.fact.usable_sessions >= MIN_USABLE_SESSIONS
        and candidate.fact.nonzero_volume_share_60d >= MIN_NONZERO_VOLUME_SHARE
        and days is not None
        and days <= MAX_CAPACITY_DAYS
        and candidate.listing_span_days is not None
        and candidate.listing_span_days >= MIN_CAPACITY_LISTING_SPAN_DAYS
    )


def _rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda item: (-item.er_annual, item.ticker))


def _market_sessions(conn: sqlite3.Connection) -> list[date]:
    rows = conn.execute(
        "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
        "GROUP BY traded_at HAVING COUNT(*) >= 2000 ORDER BY traded_at"
    ).fetchall()
    return [date.fromisoformat(str(raw)) for raw, _ in rows]


def _bars_for_window(
    conn: sqlite3.Connection, start: date, end: date, wanted: frozenset[str]
) -> dict[str, dict[date, MarketBar]]:
    rows = conn.execute(
        "SELECT ticker, traded_at, close, adjustment_close, volume, turnover_value "
        "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? ORDER BY ticker, traded_at",
        (start.isoformat(), end.isoformat()),
    )
    result: dict[str, dict[date, MarketBar]] = {}
    for ticker_raw, traded_at_raw, close, adjusted_close, volume, turnover in rows:
        ticker = str(ticker_raw)
        if ticker not in wanted:
            continue
        traded_at = date.fromisoformat(str(traded_at_raw))
        result.setdefault(ticker, {})[traded_at] = MarketBar(
            traded_at=traded_at,
            close=_optional_float(close),
            adjusted_close=_optional_float(adjusted_close),
            volume=_optional_float(volume),
            turnover_value=_optional_float(turnover),
        )
    return result


def _market_segments(conn: sqlite3.Connection, asof: date) -> dict[str, str]:
    return {
        str(ticker): str(market)
        for ticker, market in conn.execute(
            "SELECT ticker, market FROM jquants_master_snapshots WHERE snapshot_date = ?",
            (asof.isoformat(),),
        )
        if market is not None
    }


def _panel_paths(root: Path) -> list[Path]:
    paths = sorted(root.glob("panel-*.csv"))
    if not paths:
        raise CapacityStudyError(f"no panel files under {root}")
    return paths


def _rules_hash(root: Path, panel_paths: Sequence[Path]) -> str:
    hashes: set[str] = set()
    for panel_path in panel_paths:
        meta_path = panel_path.with_suffix(".meta.yaml")
        payload = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        value = payload.get("rules_hash") if isinstance(payload, Mapping) else None
        if not isinstance(value, str) or not value:
            raise CapacityStudyError(f"panel metadata lacks rules_hash: {meta_path}")
        hashes.add(value)
    if len(hashes) != 1:
        raise CapacityStudyError(f"panel rules_hash values are mixed: {sorted(hashes)}")
    return next(iter(hashes))


def _panel_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "ticker",
        "sector_33",
        "pass_screen",
        "er_annual",
        "market_cap_oku",
        "avg_turnover_oku",
        "listing_span_days",
        "population_coverage_status",
    }
    if not rows or not required.issubset(rows[0]):
        raise CapacityStudyError(f"panel lacks Stage 0 fields: {path}")
    return rows


def _asof_from_panel(path: Path) -> date:
    try:
        return date.fromisoformat(path.stem.removeprefix("panel-"))
    except ValueError as exc:
        raise CapacityStudyError(f"invalid panel filename: {path}") from exc


def _window_for(asof: date, latest_market_date: date) -> tuple[str, ...]:
    windows: list[str] = []
    if date(2020, 1, 1) <= asof <= date(2023, 7, 31):
        windows.append("1y_design")
    if date(2023, 8, 1) <= asof <= date(2025, 6, 30):
        windows.append("1y_time_holdout")
    if date(2020, 1, 1) <= asof <= date(2023, 6, 30):
        windows.append("3y_all")
    if date(2021, 7, 1) <= asof <= date(2023, 6, 30):
        windows.append("3y_postcovid")
    if require_horizon("5y").target_date(asof) <= latest_market_date:
        windows.append("5y_all")
    return tuple(windows)


def _concentration(tickers: Sequence[str]) -> dict[str, float | int | None]:
    counts = Counter(tickers)
    total = len(tickers)
    return {
        "selection_count": total,
        "unique_tickers": len(counts),
        "max_ticker_share": (max(counts.values()) / total if total else None),
    }


def _median_count(values: Sequence[int]) -> float | None:
    return float(median(values)) if values else None


def measure_stage0(*, calibration_dir: Path, market_db: Path) -> dict[str, object]:
    panel_paths = _panel_paths(calibration_dir)
    panel_rules_hash = _rules_hash(calibration_dir, panel_paths)
    conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        sessions = _market_sessions(conn)
        if not sessions:
            raise CapacityStudyError("market store has no complete sessions")
        session_index = {session: index for index, session in enumerate(sessions)}
        latest_market_date = sessions[-1]

        cohort_rows: list[dict[str, object]] = []
        all_capacity_only: list[Candidate] = []
        all_capacity_eligible: list[Candidate] = []
        incremental_by_window: dict[str, list[str]] = {
            name: []
            for name in ("1y_design", "1y_time_holdout", "3y_all", "3y_postcovid", "5y_all")
        }
        window_cohorts: Counter[str] = Counter()
        total_candidates = 0
        fact_available = 0
        top20_available = 0
        incremental_top5_available = 0
        current_top20_overlap_values: list[float] = []

        for path in panel_paths:
            asof = _asof_from_panel(path)
            index = session_index.get(asof)
            if index is None or index + 1 < SESSION_COUNT:
                raise CapacityStudyError(f"cohort lacks {SESSION_COUNT} complete sessions: {asof}")
            session_dates = sessions[index - SESSION_COUNT + 1 : index + 1]
            raw_rows = _panel_rows(path)
            screened = [
                row
                for row in raw_rows
                if _is_true(row.get("pass_screen"))
                and row.get("population_coverage_status") == "evaluated"
                and _optional_float(row.get("er_annual")) is not None
            ]
            wanted = frozenset(str(row["ticker"]) for row in screened)
            bars = _bars_for_window(conn, session_dates[0], session_dates[-1], wanted)
            segments = _market_segments(conn, asof)
            candidates: list[Candidate] = []
            for raw in screened:
                ticker = str(raw["ticker"])
                fact = _capacity_fact(session_dates=session_dates, bars=bars.get(ticker, {}))
                candidate = Candidate(
                    ticker=ticker,
                    sector=str(raw.get("sector_33") or ""),
                    market=segments.get(ticker),
                    er_annual=float(raw["er_annual"]),
                    er_carry_annual=_optional_float(raw.get("er_carry_annual")),
                    er_reversion_annual=_optional_float(raw.get("er_reversion_annual")),
                    market_cap_oku=_optional_float(raw.get("market_cap_oku")),
                    avg_turnover_oku=_optional_float(raw.get("avg_turnover_oku")),
                    listing_span_days=_optional_int(raw.get("listing_span_days")),
                    fact=fact,
                )
                candidates.append(candidate)

            total_candidates += len(candidates)
            fact_available += sum(candidate.fact.core_fact_available for candidate in candidates)
            current = _rank(candidate for candidate in candidates if _current_eligible(candidate))
            primary = _rank(
                candidate
                for candidate in candidates
                if _capacity_eligible(candidate, PRIMARY_NOTIONAL)
            )
            current_set = {candidate.ticker for candidate in current}
            primary_set = {candidate.ticker for candidate in primary}
            capacity_only = [
                candidate for candidate in primary if candidate.ticker not in current_set
            ]
            current_only = [
                candidate for candidate in current if candidate.ticker not in primary_set
            ]
            capacity_top20 = primary[:20]
            incremental_top5 = [
                candidate for candidate in capacity_top20 if candidate.ticker not in current_set
            ][:5]
            top20_available += len(capacity_top20) == 20
            incremental_top5_available += len(incremental_top5) == 5
            overlap = {candidate.ticker for candidate in current[:20]} & {
                candidate.ticker for candidate in capacity_top20
            }
            if len(current) >= 20 and len(capacity_top20) == 20:
                current_top20_overlap_values.append(len(overlap) / 20)

            all_capacity_only.extend(capacity_only)
            all_capacity_eligible.extend(primary)
            for window in _window_for(asof, latest_market_date):
                window_cohorts[window] += 1
                incremental_by_window[window].extend(
                    candidate.ticker for candidate in incremental_top5
                )

            cohort_rows.append(
                {
                    "asof": asof.isoformat(),
                    "screened_candidates": len(candidates),
                    "capacity_fact_coverage": (
                        sum(candidate.fact.core_fact_available for candidate in candidates)
                        / len(candidates)
                        if candidates
                        else None
                    ),
                    "current_eligible": len(current),
                    "capacity_eligible": {
                        name: sum(_capacity_eligible(candidate, name) for candidate in candidates)
                        for name in NOTIONALS
                    },
                    "intersection": len(current_set & primary_set),
                    "capacity_only": len(capacity_only),
                    "current_only": len(current_only),
                    "capacity_top20_count": len(capacity_top20),
                    "incremental_top5_count": len(incremental_top5),
                    "current_capacity_top20_overlap": len(overlap),
                }
            )
    finally:
        conn.close()

    cohort_count = len(cohort_rows)
    fact_coverage = fact_available / total_candidates if total_candidates else 0.0
    top20_availability = top20_available / cohort_count
    incremental_availability = incremental_top5_available / cohort_count
    window_summary = {
        name: {
            "matured_cohorts": window_cohorts[name],
            **_concentration(tickers),
        }
        for name, tickers in incremental_by_window.items()
    }
    concentration_checks: dict[str, dict[str, object]] = {}
    for name, summary in window_summary.items():
        floor = 25 if name.startswith("1y") else 15 if name.startswith("3y") else 8
        max_share = summary["max_ticker_share"]
        concentration_checks[name] = {
            "unique_ticker_floor": floor,
            "unique_ticker_pass": int(summary["unique_tickers"] or 0) >= floor,
            "max_ticker_share_ceiling": MAX_TICKER_SHARE,
            "max_ticker_share_pass": (
                isinstance(max_share, int | float) and max_share <= MAX_TICKER_SHARE
            ),
        }
    capacity_counts: list[int] = []
    for row in cohort_rows:
        counts = row["capacity_eligible"]
        if not isinstance(counts, Mapping):  # pragma: no cover - local invariant
            raise AssertionError("capacity counts must be a mapping")
        capacity_counts.append(int(counts[PRIMARY_NOTIONAL]))
    capacity_only_market_caps = [
        candidate.market_cap_oku
        for candidate in all_capacity_only
        if candidate.market_cap_oku is not None
    ]
    capacity_only_growth_40_100 = [
        candidate
        for candidate in all_capacity_only
        if candidate.market in {"グロース", "GROWTH", "マザーズ"}
        and candidate.market_cap_oku is not None
        and 40 <= candidate.market_cap_oku < 100
    ]
    carry_known = [
        candidate
        for candidate in all_capacity_only
        if candidate.er_carry_annual is not None and candidate.er_reversion_annual is not None
    ]
    sector_counts = Counter(candidate.sector for candidate in all_capacity_only if candidate.sector)
    sector_total = sum(sector_counts.values())
    carry_dominant_count = 0
    for candidate in carry_known:
        carry = candidate.er_carry_annual
        reversion = candidate.er_reversion_annual
        assert carry is not None
        assert reversion is not None
        carry_dominant_count += carry > reversion
    stage0_checks: dict[str, object] = {
        "capacity_fact_coverage": {
            "value": fact_coverage,
            "floor": MIN_FACT_COVERAGE,
            "pass": fact_coverage >= MIN_FACT_COVERAGE,
        },
        "capacity_top20_availability": {
            "value": top20_availability,
            "floor": MIN_TOP20_AVAILABILITY,
            "pass": top20_availability >= MIN_TOP20_AVAILABILITY,
        },
        "incremental_top5_availability": {
            "value": incremental_availability,
            "floor": MIN_INCREMENTAL_TOP5_AVAILABILITY,
            "pass": incremental_availability >= MIN_INCREMENTAL_TOP5_AVAILABILITY,
        },
        "windows": concentration_checks,
        "trading_unit_pit": {
            "value": TRADING_UNIT,
            "pass": min(_asof_from_panel(path) for path in panel_paths) >= date(2018, 10, 1),
        },
    }
    overall = all(
        check.get("pass") is True
        for key, check in stage0_checks.items()
        if key != "windows" and isinstance(check, Mapping)
    ) and all(
        checks["unique_ticker_pass"] is True and checks["max_ticker_share_pass"] is True
        for checks in concentration_checks.values()
    )
    return {
        "schema_version": 1,
        "study": "capacity_native_universe",
        "stage": "outcome_free_feasibility",
        "outcome_data_read": False,
        "value_tier": "T1",
        "inputs": {
            "panel_rules_hash": panel_rules_hash,
            "panel_cohort_count": cohort_count,
            "panel_asof_start": _asof_from_panel(panel_paths[0]).isoformat(),
            "panel_asof_end": _asof_from_panel(panel_paths[-1]).isoformat(),
            "latest_market_date": latest_market_date.isoformat(),
            "forward_inputs": [],
        },
        "contract": {
            "trading_unit": {
                "shares": TRADING_UNIT,
                "effective_from": "2018-10-01",
                "source_url": JPX_TRADING_UNIT_SOURCE,
            },
            "sessions": SESSION_COUNT,
            "turnover_percentile": "nearest_rank_p20_over_60_market_sessions_missing_as_zero",
            "minimum_lot": "unadjusted_asof_close_times_100_shares",
            "return_price_basis": "jquants_adjustment_close",
            "notionals_yen": NOTIONALS,
            "primary_notional": PRIMARY_NOTIONAL,
            "participation_rate": PARTICIPATION_RATE,
            "max_capacity_days": MAX_CAPACITY_DAYS,
            "min_usable_sessions": MIN_USABLE_SESSIONS,
            "min_nonzero_volume_share": MIN_NONZERO_VOLUME_SHARE,
            "min_listing_span_days": MIN_CAPACITY_LISTING_SPAN_DAYS,
        },
        "summary": {
            "screened_candidate_observations": total_candidates,
            "capacity_fact_coverage": fact_coverage,
            "primary_capacity_eligible_count": {
                "min": min(capacity_counts),
                "median": _median_count(capacity_counts),
                "latest": capacity_counts[-1],
            },
            "capacity_top20_availability": top20_availability,
            "incremental_top5_availability": incremental_availability,
            "current_capacity_top20_overlap": _distribution(current_top20_overlap_values),
            "capacity_only_observations": len(all_capacity_only),
            "capacity_only_unique_tickers": len(
                {candidate.ticker for candidate in all_capacity_only}
            ),
            "capacity_only_market_cap_oku": _distribution(capacity_only_market_caps),
            "capacity_only_growth_market_40_to_100_oku_share": (
                len(capacity_only_growth_40_100) / len(all_capacity_only)
                if all_capacity_only
                else None
            ),
            "capacity_only_carry_dominant_share": (
                carry_dominant_count / len(carry_known) if carry_known else None
            ),
            "capacity_only_sector_hhi": (
                sum((count / sector_total) ** 2 for count in sector_counts.values())
                if sector_total
                else None
            ),
            "minimum_lot_yen": _distribution(
                candidate.fact.minimum_lot_yen
                for candidate in all_capacity_eligible
                if candidate.fact.minimum_lot_yen is not None
            ),
            "capacity_days_at_1pct_standard": _distribution(
                days
                for candidate in all_capacity_eligible
                if (days := candidate.fact.capacity_days_by_notional[PRIMARY_NOTIONAL]) is not None
            ),
            "zero_return_share_60d": _distribution(
                value
                for candidate in all_capacity_eligible
                if (value := candidate.fact.zero_return_share_60d) is not None
            ),
            "no_trade_share_60d": _distribution(
                candidate.fact.no_trade_share_60d for candidate in all_capacity_eligible
            ),
            "amihud_median_60d": _distribution(
                value
                for candidate in all_capacity_eligible
                if (value := candidate.fact.amihud_illiquidity_median_60d) is not None
            ),
        },
        "windows": window_summary,
        "cohorts": cohort_rows,
        "stage0_sufficiency": {"checks": stage0_checks, "overall_pass": overall},
        "limitations": [
            (
                "listing_span_days is capped by the 1200-day panel input window "
                "and is a first-bar proxy"
            ),
            (
                "historical JPX regulation flags are unavailable in the panel; "
                "both policies share the same empty-flag replay contract"
            ),
            "Amihud and zero-return are diagnostics, not membership gates",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = measure_stage0(calibration_dir=args.calibration_dir, market_db=args.market_db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    args.out.write_text(rendered, encoding="utf-8")
    if stdout is not None:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
