"""Measure candidate-evaluation and holding-review latency for the daily delta."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from statistics import median
from typing import TextIO
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.position.ledger import replay_events_through
from baibai_engine.read_api.position import portfolio_ledger_document
from baibai_engine.read_api.research import (
    list_holding_review_publications,
    list_thesis_publications,
)
from baibai_engine.read_api.screening import (
    screening_run_asof_dates,
    screening_selection_payloads,
)
from baibai_engine.read_api.shortlist import list_shortlist_payloads
from baibai_engine.read_api.sqlite import read_rows
from baibai_engine.screening.store_readiness import unreadable_store_reason

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_ASOF = date(2026, 8, 2)


class DailyDeltaMeasurementError(ValueError):
    """Raised when canonical inputs cannot support a truthful measurement."""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _as_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise DailyDeltaMeasurementError("publication timestamps must include a timezone")
    return parsed


def _jst_date(value: object) -> date:
    return _as_datetime(value).astimezone(JST).date()


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _longlist(selection: Mapping[str, object]) -> list[Mapping[str, object]] | None:
    payload = selection.get("payload")
    rows = payload.get("longlist") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        return None
    parsed = _mapping_rows(rows)
    return parsed if len(parsed) == len(rows) else None


def _selection_instant(selection: Mapping[str, object]) -> tuple[datetime, str]:
    return _as_datetime(selection["created_at"]), str(selection.get("selection_id", ""))


def _shortlists_by_selection(
    shortlists: Sequence[Mapping[str, object]], *, asof: date
) -> dict[str, list[Mapping[str, object]]]:
    indexed: dict[str, list[Mapping[str, object]]] = {}
    for shortlist in shortlists:
        if _jst_date(shortlist["published_at"]) > asof:
            continue
        selection_id = shortlist.get("selection_id")
        if selection_id:
            indexed.setdefault(str(selection_id), []).append(shortlist)
    return indexed


def _daily_pool(
    selections: Sequence[Mapping[str, object]],
    shortlists_by_selection: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[Mapping[str, object] | None, str]:
    with_longlist = [item for item in selections if _longlist(item) is not None]
    bound_ids = {
        str(item.get("selection_id", ""))
        for item in selections
        if str(item.get("selection_id", "")) in shortlists_by_selection
    }
    if len(bound_ids) > 1:
        return None, "ambiguous_shortlist_bindings"
    if len(bound_ids) == 1:
        bound_id = next(iter(bound_ids))
        bound = next(item for item in selections if str(item.get("selection_id")) == bound_id)
        return (
            (bound, "shortlist_bound")
            if _longlist(bound) is not None
            else (
                None,
                "bound_selection_without_longlist",
            )
        )
    if not with_longlist:
        return None, "longlist_missing"
    return max(with_longlist, key=_selection_instant), "latest_longlist"


def _evaluations(
    shortlists: Sequence[Mapping[str, object]], *, asof: date
) -> dict[str, list[tuple[date, date, str, str]]]:
    result: dict[str, list[tuple[date, date, str, str]]] = {}
    for shortlist in shortlists:
        published = _jst_date(shortlist["published_at"])
        evaluated_asof = _as_date(shortlist["as_of"])
        if published > asof:
            continue
        shortlist_id = str(shortlist.get("shortlist_id", ""))
        for entry in _mapping_rows(shortlist.get("entries")):
            decision = str(entry.get("decision", ""))
            ticker = entry.get("ticker")
            if ticker and decision in {"selected", "rejected"}:
                result.setdefault(str(ticker), []).append(
                    (published, evaluated_asof, shortlist_id, decision)
                )
    for rows in result.values():
        rows.sort()
    return result


def _delay_summary(values: Sequence[int]) -> dict[str, object]:
    buckets = {"0d": 0, "1d": 0, "2_3d": 0, "4_7d": 0, "8d_plus": 0}
    for value in values:
        if value == 0:
            buckets["0d"] += 1
        elif value == 1:
            buckets["1d"] += 1
        elif value <= 3:
            buckets["2_3d"] += 1
        elif value <= 7:
            buckets["4_7d"] += 1
        else:
            buckets["8d_plus"] += 1
    return {
        "count": len(values),
        "min_days": min(values) if values else None,
        "median_days": median(values) if values else None,
        "max_days": max(values) if values else None,
        "buckets": buckets,
    }


def build_candidate_measurement(
    *,
    run_dates: Sequence[date],
    selections: Sequence[Mapping[str, object]],
    shortlists: Sequence[Mapping[str, object]],
    asof: date,
) -> dict[str, object]:
    """Measure first-observed longlist membership without hiding retention gaps."""

    dates = sorted({day for day in run_dates if day <= asof})
    by_date: dict[date, list[Mapping[str, object]]] = {day: [] for day in dates}
    for selection in selections:
        day = _as_date(selection["as_of_date"])
        if day in by_date and selection.get("publication_kind") == "machine":
            by_date[day].append(selection)
    shortlist_index = _shortlists_by_selection(shortlists, asof=asof)
    snapshots: list[dict[str, object]] = []
    pools: list[tuple[date, set[str]]] = []
    for day in dates:
        chosen, status = _daily_pool(by_date[day], shortlist_index)
        pool_rows = None if chosen is None else _longlist(chosen)
        tickers = (
            set()
            if pool_rows is None
            else {str(row["ticker"]) for row in pool_rows if row.get("ticker")}
        )
        snapshots.append(
            {
                "as_of": day.isoformat(),
                "status": status,
                "machine_selection_count": len(by_date[day]),
                "selection_id": None if chosen is None else chosen.get("selection_id"),
                "longlist_size": None if pool_rows is None else len(tickers),
            }
        )
        if pool_rows is not None:
            pools.append((day, tickers))

    first_observed: dict[str, date] = {}
    for day, tickers in pools:
        for ticker in tickers:
            first_observed.setdefault(ticker, day)
    evaluation_index = _evaluations(shortlists, asof=asof)
    coverage_start = dates[0] if dates else None
    complete_dates = {day for day, _tickers in pools}
    ticker_rows: list[dict[str, object]] = []
    exact_delays: list[int] = []
    captured = 0
    for ticker, first_day in sorted(first_observed.items()):
        prior_dates = [day for day in dates if day < first_day]
        left_censored = first_day == coverage_start or any(
            day not in complete_dates for day in prior_dates
        )
        later = [
            item
            for item in evaluation_index.get(ticker, [])
            if item[0] >= first_day and item[1] >= first_day
        ]
        evaluation = later[0] if later else None
        delay = None if evaluation is None else (evaluation[0] - first_day).days
        if evaluation is not None:
            captured += 1
            if not left_censored:
                exact_delays.append(delay or 0)
        ticker_rows.append(
            {
                "ticker": ticker,
                "first_observed_pool_date": first_day.isoformat(),
                "first_seen_status": "left_censored" if left_censored else "exact",
                "evaluation_status": "recorded" if evaluation is not None else "right_censored",
                "evaluation_recorded_date": (
                    None if evaluation is None else evaluation[0].isoformat()
                ),
                "shortlist_id": None if evaluation is None else evaluation[2],
                "decision": None if evaluation is None else evaluation[3],
                "delay_days": delay if evaluation is not None and not left_censored else None,
                "delay_lower_bound_days": delay
                if evaluation is not None and left_censored
                else None,
                "right_censored_days": ((asof - first_day).days if evaluation is None else None),
            }
        )
    denominator = len(first_observed)
    return {
        "source_basis": "screening_selection.payload.longlist",
        "evaluation_time_basis": "shortlist.published_at_jst_date_proxy",
        "coverage": {
            "run_record_start": None if not dates else dates[0].isoformat(),
            "run_record_end": None if not dates else dates[-1].isoformat(),
            "run_date_count": len(dates),
            "complete_longlist_snapshot_count": len(pools),
            "missing_or_ambiguous_snapshot_count": len(dates) - len(pools),
            "r2_candidate_history_excluded": True,
            "r2_reason": "candidate history does not preserve explicit longlist membership",
        },
        "pool_unique_ticker_count": denominator,
        "evaluated_ticker_count": captured,
        "capture_rate_pct": None if denominator == 0 else round(captured * 100 / denominator, 1),
        "exact_first_seen_count": sum(row["first_seen_status"] == "exact" for row in ticker_rows),
        "left_censored_first_seen_count": sum(
            row["first_seen_status"] == "left_censored" for row in ticker_rows
        ),
        "exact_delay_distribution": _delay_summary(exact_delays),
        "snapshots": snapshots,
        "tickers": ticker_rows,
    }


def _latest_thesis_by_ticker(
    theses: Sequence[Mapping[str, object]], *, tickers: set[str], asof: date
) -> dict[str, Mapping[str, object]]:
    eligible = [
        item
        for item in theses
        if str(item.get("ticker", "")) in tickers and _jst_date(item["published_at"]) <= asof
    ]
    eligible.sort(
        key=lambda item: (
            _as_datetime(item["published_at"]),
            str(item.get("thesis_id", "")),
        ),
        reverse=True,
    )
    result: dict[str, Mapping[str, object]] = {}
    for item in eligible:
        result.setdefault(str(item["ticker"]), item)
    return result


def _fair_value(thesis: Mapping[str, object]) -> float | None:
    payload = thesis.get("payload")
    estimates = payload.get("estimates") if isinstance(payload, Mapping) else None
    raw = estimates.get("current_fair_value_yen") if isinstance(estimates, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _reviews_by_ticker(
    reviews: Sequence[Mapping[str, object]], *, asof: date
) -> dict[str, list[Mapping[str, object]]]:
    result: dict[str, list[Mapping[str, object]]] = {}
    for review in reviews:
        review_day = _as_date(review["as_of"])
        if review_day <= asof:
            result.setdefault(str(review.get("ticker", "")), []).append(review)
    for items in result.values():
        items.sort(key=lambda item: (_as_date(item["as_of"]), str(item["holding_review_id"])))
    return result


def build_holding_measurement(
    *,
    open_tickers: Sequence[str],
    ledger_asof: datetime,
    theses: Sequence[Mapping[str, object]],
    reviews: Sequence[Mapping[str, object]],
    bars_by_ticker: Mapping[str, Sequence[Mapping[str, object]]],
    asof: date,
) -> dict[str, object]:
    """Measure first FV reach and the next holding review on daily-delta price basis."""

    tickers = set(open_tickers)
    latest_theses = _latest_thesis_by_ticker(theses, tickers=tickers, asof=asof)
    review_index = _reviews_by_ticker(reviews, asof=asof)
    rows: list[dict[str, object]] = []
    reviewed_delays: list[int] = []
    triggered = 0
    reviewed = 0
    for ticker in sorted(tickers):
        thesis = latest_theses.get(ticker)
        if thesis is None:
            rows.append({"ticker": ticker, "status": "thesis_missing"})
            continue
        fv = _fair_value(thesis)
        published = _jst_date(thesis["published_at"])
        base = {
            "ticker": ticker,
            "thesis_id": thesis.get("thesis_id"),
            "thesis_published_date": published.isoformat(),
            "fair_value_yen": fv,
        }
        if fv is None:
            rows.append({**base, "status": "fair_value_missing"})
            continue
        bars = [
            bar
            for bar in bars_by_ticker.get(ticker, [])
            if published <= _as_date(bar["traded_at"]) <= asof
        ]
        if not bars:
            rows.append({**base, "status": "price_missing"})
            continue
        if any(
            bar.get("adjustment_factor") is not None and float(str(bar["adjustment_factor"])) != 1.0
            for bar in bars
        ):
            rows.append({**base, "status": "corporate_action_unresolved"})
            continue
        reached = next(
            (
                bar
                for bar in sorted(bars, key=lambda item: _as_date(item["traded_at"]))
                if bar.get("close") is not None and float(str(bar["close"])) >= fv
            ),
            None,
        )
        if reached is None:
            rows.append(
                {
                    **base,
                    "status": "not_reached",
                    "market_observation_end": max(
                        _as_date(bar["traded_at"]) for bar in bars
                    ).isoformat(),
                }
            )
            continue
        triggered += 1
        reached_day = _as_date(reached["traded_at"])
        later_reviews = [
            item for item in review_index.get(ticker, []) if _as_date(item["as_of"]) >= reached_day
        ]
        review = later_reviews[0] if later_reviews else None
        if review is None:
            rows.append(
                {
                    **base,
                    "status": "triggered_unreviewed",
                    "fv_reached_date": reached_day.isoformat(),
                    "reached_close_yen": float(str(reached["close"])),
                    "right_censored_days": (asof - reached_day).days,
                }
            )
            continue
        reviewed += 1
        review_day = _as_date(review["as_of"])
        delay = (review_day - reached_day).days
        reviewed_delays.append(delay)
        rows.append(
            {
                **base,
                "status": "reviewed",
                "fv_reached_date": reached_day.isoformat(),
                "reached_close_yen": float(str(reached["close"])),
                "holding_review_id": review.get("holding_review_id"),
                "holding_review_date": review_day.isoformat(),
                "delay_days": delay,
            }
        )
    return {
        "population_basis": "open_lots_at_canonical_ledger_head",
        "price_basis": "unadjusted_close",
        "ledger_head": ledger_asof.isoformat(),
        "post_ledger_coverage_gap": (
            None
            if ledger_asof.astimezone(JST).date() >= asof
            else {
                "start": date.fromordinal(
                    ledger_asof.astimezone(JST).date().toordinal() + 1
                ).isoformat(),
                "end": asof.isoformat(),
                "status": "position_state_unobserved",
            }
        ),
        "open_holding_count": len(tickers),
        "fv_trigger_count": triggered,
        "reviewed_after_trigger_count": reviewed,
        "capture_rate_pct": None if triggered == 0 else round(reviewed * 100 / triggered, 1),
        "review_delay_distribution": _delay_summary(reviewed_delays),
        "holdings": rows,
    }


def _market_bars(
    path: Path, tickers: Sequence[str], *, asof: date
) -> dict[str, list[dict[str, object]]]:
    if not tickers:
        return {}
    unique = sorted(set(tickers))
    placeholders = ",".join("?" for _ in unique)
    rows = read_rows(
        path,
        f"""
        SELECT ticker, traded_at, close, adjustment_factor
        FROM jquants_daily_bars
        WHERE ticker IN ({placeholders}) AND traded_at <= ?
        ORDER BY ticker, traded_at
        """,  # nosec B608
        [*unique, asof.isoformat()],
    )
    result: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        result.setdefault(str(row["ticker"]), []).append(
            {
                "traded_at": str(row["traded_at"]),
                "close": row["close"],
                "adjustment_factor": row["adjustment_factor"],
            }
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_measurement(
    *,
    db_path: Path,
    runs_db_path: Path,
    market_db_path: Path,
    asof: date,
) -> dict[str, object]:
    ledger = portfolio_ledger_document(db_path)
    if ledger is None:
        raise DailyDeltaMeasurementError("canonical ledger is unavailable")
    observation_end = datetime.combine(asof, time.max, tzinfo=JST)
    ledger_instant = min(observation_end, ledger.as_of)
    state = replay_events_through(ledger.events, ledger_instant)
    open_tickers = [
        ticker for ticker, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
    ]
    run_dates = screening_run_asof_dates(runs_db_path, limit=10_000)
    shortlists = list_shortlist_payloads(db_path)
    return {
        "kind": "daily-delta-effect-baseline",
        "measurement_asof": asof.isoformat(),
        "day_basis": "jst_calendar_days",
        "input_sha256": {
            "application_db": _sha256(db_path),
            "runs_db": _sha256(runs_db_path),
            "market_db": _sha256(market_db_path),
        },
        "candidate": build_candidate_measurement(
            run_dates=run_dates,
            selections=screening_selection_payloads(runs_db_path),
            shortlists=shortlists,
            asof=asof,
        ),
        "holding": build_holding_measurement(
            open_tickers=open_tickers,
            ledger_asof=ledger.as_of,
            theses=list_thesis_publications(db_path),
            reviews=list_holding_review_publications(db_path),
            bars_by_ticker=_market_bars(market_db_path, open_tickers, asof=asof),
            asof=asof,
        ),
    }


def measure_command(
    *,
    db_path: Path,
    runs_db_path: Path,
    market_db_path: Path,
    asof: date,
    output_path: Path | None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    for name, path in (("application", db_path), ("runs", runs_db_path)):
        if not path.is_file():
            print(f"daily-delta-baseline: {name} store is missing: {path}", file=sys.stderr)
            return 1
    unreadable = unreadable_store_reason(market_db_path)
    if unreadable is not None:
        print(f"daily-delta-baseline: market store: {unreadable}", file=sys.stderr)
        return 1
    try:
        payload = build_measurement(
            db_path=db_path,
            runs_db_path=runs_db_path,
            market_db_path=market_db_path,
            asof=asof,
        )
    except (DailyDeltaMeasurementError, ValueError) as error:
        print(f"daily-delta-baseline: {error}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path is None:
        print(text, file=out)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"daily-delta-baseline: wrote {output_path}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/app/baibai.sqlite"))
    parser.add_argument("--runs-db", type=Path, default=Path("data/screening/runs.sqlite"))
    parser.add_argument("--market-db", type=Path, default=Path("data/screening/market.sqlite"))
    parser.add_argument("--as-of", type=_parse_date, default=DEFAULT_ASOF)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return measure_command(
        db_path=args.db,
        runs_db_path=args.runs_db,
        market_db_path=args.market_db,
        asof=args.as_of,
        output_path=args.out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
