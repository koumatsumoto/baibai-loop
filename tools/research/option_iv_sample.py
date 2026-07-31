"""Measure the option-IV fear readings on a uniform sample of trading days.

The readings are quoted against thresholds, and a threshold is only as honest as
the sample the quantiles came from. Sampling by calendar step rather than by
availability keeps a volatile stretch from carrying more weight than a quiet one
just because it is more interesting; a sample drawn around the days a reader
remembers would raise every upper quantile.

Writes one CSV row per sampled day so the quantiles can be regenerated and the
window extended, and prints the quantiles the reading rules quote.

    uv run python tools/research/option_iv_sample.py --start 2018-01-01 \
        --end 2026-07-31 --step 5 --out reports/data/option-iv-sample.csv
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
    FearReadings,
    fear_readings,
    quotes_from_records,
)

_QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def business_days(start: date, end: date, step: int) -> Iterator[date]:
    """Every `step`-th weekday, so the sample tracks the calendar, not the news."""
    cursor, taken = start, 0
    while cursor <= end:
        if cursor.weekday() < 5:
            if taken % step == 0:
                yield cursor
            taken += 1
        cursor += timedelta(days=1)


def read_day(client: object, day: date) -> FearReadings | None:
    method = getattr(client, "get_drv_bars_daily_opt_225")
    frame = method(date_yyyymmdd=day.strftime("%Y%m%d"))
    if frame is None or frame.empty:
        return None
    records = [row for row in frame.to_dict(orient="records") if isinstance(row, dict)]
    return fear_readings(quotes_from_records(records, day), day)


def quantiles(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        f"p{int(q * 100):02d}": ordered[min(int(q * len(ordered)), len(ordered) - 1)]
        for q in _QUANTILES
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--step", type=int, default=5, help="sample every Nth business day")
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
    rows: list[dict[str, object]] = []
    for day in business_days(args.start, args.end, args.step):
        reading = read_day(client, day)
        if reading is None:
            continue
        rows.append(
            {
                "day": day.isoformat(),
                "iv_30d": reading.iv_30d,
                "iv_skew": reading.iv_skew,
                "iv_term": reading.iv_term,
            }
        )
        print(f"{day} {reading.iv_30d} {reading.iv_skew} {reading.iv_term}", flush=True)

    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["day", "iv_30d", "iv_skew", "iv_term"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nsampled {len(rows)} trading day(s) every {args.step} business day(s)")
    for field in ("iv_30d", "iv_skew", "iv_term"):
        values = [float(row[field]) for row in rows if row[field] is not None]
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
