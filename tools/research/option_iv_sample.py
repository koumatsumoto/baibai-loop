"""Measure the option-IV fear readings on a uniform sample of trading days.

The readings are quoted against thresholds, and a threshold is only as honest as
the sample the quantiles came from. Sampling by calendar step rather than by
availability keeps a volatile stretch from carrying more weight than a quiet one
just because it is more interesting; a sample drawn around the days a reader
remembers would raise every upper quantile.

Writes one CSV row per sampled day so the quantiles can be regenerated and the
window extended, and prints the quantiles the reading rules quote.

    uv run python tools/research/option_iv_sample.py --start 2018-01-01 \
        --end 2026-07-31 --step 4 --out reports/data/option-iv-sample.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.foundation.env import load_project_env
from baibai_engine.macro.indicators.providers.option_iv import (
    MIN_DAYS_TO_EXPIRY,
    FearReadings,
    fear_readings,
    quotes_from_records,
)

_QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
_COLUMNS = ("day", "iv_30d", "iv_skew", "iv_term", "near_days", "far_days")


def business_days(start: date, end: date, step: int) -> Iterator[date]:
    """Every `step`-th weekday, so the sample tracks the calendar, not the news.

    A step that divides the five-day week lands on one weekday forever. The weekday
    premium measured here is small — a Monday `iv_30d` runs 0.9 points above the rest
    at the median — but a one-weekday sample cannot be checked for it at all, and a
    reader has no way to tell a small bias from a large one. Such steps are refused
    rather than silently producing a sample that cannot be audited.
    """
    if step % 5 == 0:
        raise ValueError(f"step {step} is a multiple of the trading week and fixes the weekday")
    cursor, taken = start, 0
    while cursor <= end:
        if cursor.weekday() < 5:
            if taken % step == 0:
                yield cursor
            taken += 1
        cursor += timedelta(days=1)


def read_day(client: object, day: date) -> tuple[FearReadings, list[int]] | None:
    """The day's readings, with the maturities they were read off.

    The maturities are recorded because the two difference readings depend on which
    contracts were in front: a quantile pooled across the settlement cycle mixes
    maturities unless the sample says which ones it drew. They are derived here from
    the expiry cutoff rather than reported by the readings, which carry only what the
    series store keeps.
    """
    method = getattr(client, "get_drv_bars_daily_opt_225", None)
    if not callable(method):
        raise RuntimeError("jquantsapi.ClientV2 has no callable get_drv_bars_daily_opt_225")
    frame = method(date_yyyymmdd=day.strftime("%Y%m%d"))
    if frame is None or frame.empty:
        return None
    records = [row for row in frame.to_dict(orient="records") if isinstance(row, dict)]
    quotes = quotes_from_records(records, day)
    maturities = sorted(
        {days for quote in quotes if (days := (quote.expiry - day).days) >= MIN_DAYS_TO_EXPIRY}
    )
    return fear_readings(quotes, day), maturities


def quantiles(values: Sequence[float]) -> dict[str, float]:
    """The order statistic at rank floor(q·n), counting from zero.

    Every value the reading rules quote comes out of here, so the convention is
    stated rather than assumed: each quantile is a reading the market actually made,
    never an average of two and never extrapolated past the largest one. Every
    fraction is below 1, so floor(q·n) is an index into any non-empty series.
    """
    ordered = sorted(values)
    return {f"p{int(q * 100):02d}": ordered[int(q * len(ordered))] for q in _QUANTILES}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--step", type=int, default=4, help="sample every Nth business day")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    load_project_env()
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("JQUANTS_API_KEY is required", file=sys.stderr)
        return 2
    import jquantsapi

    client = jquantsapi.ClientV2(api_key=api_key)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str | float | int | None]] = []
    for day in business_days(args.start, args.end, args.step):
        read = read_day(client, day)
        if read is None:
            continue
        reading, maturities = read
        rows.append(
            {
                "day": day.isoformat(),
                "iv_30d": reading.iv_30d,
                "iv_skew": reading.iv_skew,
                "iv_term": reading.iv_term,
                "near_days": maturities[0] if maturities else None,
                "far_days": maturities[1] if len(maturities) > 1 else None,
            }
        )
        print(
            f"{day} {reading.iv_30d} {reading.iv_skew} {reading.iv_term} {maturities[:2]}",
            flush=True,
        )

    with args.out.open("w", newline="", encoding="utf-8") as handle:
        # csv writes CRLF by default; the sample is committed, and a file whose line
        # endings git normalises comes back changed every time it is regenerated.
        writer = csv.DictWriter(handle, fieldnames=list(_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nsampled {len(rows)} trading day(s) every {args.step} business day(s)")
    for field in ("iv_30d", "iv_skew", "iv_term"):
        values = [value for row in rows if isinstance(value := row[field], float)]
        present = f"{len(values)}/{len(rows)}"
        if not values:
            print(f"{field:8s} present={present}")
            continue
        stats = quantiles(values)
        rendered = " ".join(f"{name}={value:.2f}" for name, value in stats.items())
        print(f"{field:8s} present={present} mean={statistics.fmean(values):.2f} {rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
