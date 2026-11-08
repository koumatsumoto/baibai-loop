"""source入力の取得・再開・coverage保守。モデル生成や候補選定はしない。"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.market_rules import (
    CALENDAR_FORWARD_DAYS,
    CALENDAR_LOOKBACK_DAYS,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.primitives.market_windows import (
    FINANCIAL_HISTORY_DAYS,
    FINANCIAL_REVISION_OVERLAP_DAYS,
)
from baibai_engine.storage.sqlite.source_cache.coverage import (
    count_overlapping_source_coverage,
    count_source_coverage,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    source_coverage_sources,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection
from baibai_jobs.maintenance.market.providers import JQuantsMaintenance, ProviderBundle

# 明示bootstrapで使う既存入力窓。通常生成の必要窓とは独立に維持する。
BARS_INPUT_WINDOW_DAYS = 1200
FIN_INPUT_WINDOW_DAYS = 730
NORMALIZED_EPS_HISTORY_WINDOW_DAYS = FINANCIAL_HISTORY_DAYS


def _maintenance(providers: ProviderBundle) -> JQuantsMaintenance:
    if providers.jquants_refresh is None:
        raise ValueError("this command requires explicit J-Quants maintenance")
    return providers.jquants_refresh


# Incremental calendar acquisition extends through the next several weeks.
# Full historical calendar coverage is prepared separately; this window does not
# establish the long history needed by momentum or bootstrap a missing baseline.
# Readers apply their own date ranges; non-business days do not stop model generation.
_CALENDAR_BOOTSTRAP_BACKWARD_DAYS = CALENDAR_LOOKBACK_DAYS
_CALENDAR_BOOTSTRAP_FORWARD_DAYS = CALENDAR_FORWARD_DAYS


def invalidate_coverage_command(
    *,
    sqlite_path: Path,
    source: str,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Invalidate fetch coverage without deleting the stored observations.

    Sources governed by coverage windows, including daily bars, will be fetched
    again when required. Stored observations alone do not establish acquisition
    coverage. With start/end, only overlapping coverage windows are removed.
    Source retention, the active plan and availability determine whether a later
    fetch can succeed.
    """
    out = stdout if stdout is not None else sys.stdout
    if (start is None) != (end is None):
        print("--start and --end must be given together", file=sys.stderr)
        return 1
    if start is not None and end is not None and start > end:
        print(
            f"--start {start.isoformat()} must not be after --end {end.isoformat()}",
            file=sys.stderr,
        )
        return 1
    if not sqlite_path.exists():
        print(f"SQLite cache not found: {sqlite_path}", file=sys.stderr)
        return 1
    try:
        conn = open_connection(sqlite_path)
    except (SQLiteSchemaError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        known = source_coverage_sources(conn)
        if source not in known:
            known_list = ", ".join(known) if known else "(none)"
            print(
                f"unknown coverage source {source!r}; known sources: {known_list}",
                file=sys.stderr,
            )
            return 1
        if start is not None and end is not None:
            window = f"{start.isoformat()}..{end.isoformat()}"
            target = count_overlapping_source_coverage(conn, source, start, end)
        else:
            window = "all windows"
            target = count_source_coverage(conn, source)
        print(
            f"invalidate-coverage: removing {target} source_coverage row(s) "
            f"for source={source} ({window})",
            file=out,
        )
        if start is not None and end is not None:
            delete_overlapping_source_coverage(conn, source, start, end)
        else:
            delete_source_coverage(conn, source)
        conn.commit()
    finally:
        conn.close()
    print(
        "invalidate-coverage done; stored observations unchanged",
        file=out,
    )
    return 0


def _calendar_year_spans(start: date, end: date) -> tuple[tuple[date, date], ...]:
    """Split `[start, end]` at calendar-year boundaries.

    The interior boundaries come from the calendar rather than from `start`, so runs
    that name different first dates still request the same interior spans and reuse
    each other's fetch chunks.
    """
    spans: list[tuple[date, date]] = []
    span_start = start
    while span_start <= end:
        span_end = min(date(span_start.year, 12, 31), end)
        spans.append((span_start, span_end))
        span_start = span_end + timedelta(days=1)
    return tuple(spans)


def backfill_history_command(
    *,
    start: date,
    end: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Fill the range sources over an explicit window.

    ``bootstrap-cache`` derives its windows from one as-of, which is right when the
    question is "can this run proceed" and wrong when the question is "does the store
    reach back far enough". Repeated as-of runs revisit overlapping price and
    financial windows; naming the window once requests one bounded pass, and
    coverage merges it with what is already held.

    Source-specific failures are independent, but exhausted shared quota ends the
    pass. Chunks already covered are skipped, so a run interrupted after hours
    resumes where it stopped rather than starting over.

    Bars come first: the month-end grid that ``backfill-master`` fills is derived from
    the bar store, so snapshots for months without bars cannot be requested yet.

    The store this writes to is named on the first line, and whether it already held
    anything. The path comes from the working directory, so a run started from the
    wrong one otherwise spends hours filling a store nobody reads and reports success.
    """
    out = stdout if stdout is not None else sys.stdout
    refresh = _maintenance(providers)
    window = f"{start.isoformat()}..{end.isoformat()}"
    state = "existing" if sqlite_path.exists() else "new"
    print(f"backfill-history store: {sqlite_path} ({state})", file=out, flush=True)
    print(f"backfill-history start: {window}", file=out, flush=True)
    sources: tuple[tuple[str, Callable[[date, date], int], bool], ...] = (
        ("daily_bars", refresh.ensure_daily_bars, True),
        ("fin_summaries", refresh.ensure_fin_summaries, True),
        ("market_calendar", refresh.ensure_market_calendar, False),
    )
    failures: list[str] = []
    for name, fetch, by_year in sources:
        print(f"backfill-history {name}: {window} start", file=out, flush=True)
        # Bound historical source planning and progress by calendar year.
        spans = _calendar_year_spans(start, end) if by_year else ((start, end),)
        count = 0
        try:
            for span_start, span_end in spans:
                count += fetch(span_start, span_end)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            SQLiteSchemaError,
            sqlite3.Error,
        ) as exc:
            print(
                f"backfill-history {name}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            failures.append(name)
            if isinstance(exc, JQuantsRateLimitError):
                return 1
            continue
        print(f"backfill-history {name}: {count} row(s)", file=out, flush=True)
    if failures:
        print(
            f"backfill-history: {len(failures)} source(s) failed: {', '.join(failures)}",
            file=sys.stderr,
        )
        return 1
    print(f"backfill-history done: {window}", file=out, flush=True)
    return 0


FIN_SUMMARY_REVISION_OVERLAP_DAYS = FINANCIAL_REVISION_OVERLAP_DAYS
"""How far back a bootstrap re-reads financial summaries it already has.

A correction is disclosed on its own date and arrives as a new row, so past days
do not need re-reading for that. What this window catches is a filing the provider
publishes for a date the batch has already fetched. One week spans a full
disclosure cadence including a weekend and a closure.

The store's own coverage is what decides how far back a *gap* is fetched, so this
window is never the thing that recovers a stopped batch — using it that way would
silently miss everything older than seven days the moment an outage ran longer.
"""


def bootstrap_cache_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    stdout: TextIO | None = None,
    source_group: str = "all",
) -> int:
    out = stdout if stdout is not None else sys.stdout
    print(f"bootstrap-cache start: asof={asof_date.isoformat()}", file=out, flush=True)
    failed = False
    # Fixed acquisition groups share the existing providers/cache. Each writer
    # owns its validated chunk commits; no restoration or second copy is made.
    groups = {
        "jquants-market": _bootstrap_market_cache,
        "jquants-financial": _bootstrap_financial_cache,
        "jpx": _bootstrap_jpx_cache,
        "jpx-earnings": _bootstrap_jpx_earnings,
        "jpx-regulations": _bootstrap_jpx_regulations,
    }
    if source_group != "all" and source_group not in groups:
        raise ValueError("unsupported acquisition source group")
    selected = (
        ("jquants-market", "jquants-financial", "jpx") if source_group == "all" else (source_group,)
    )
    jquants_rate_limited = False
    for group in selected:
        if jquants_rate_limited and group in {"jquants-market", "jquants-financial"}:
            print(f"{group}: skipped after J-Quants rate limit", file=sys.stderr)
            continue
        acquire = groups[group]
        try:
            acquire(asof_date, providers, sqlite_path, out)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            JPXProviderError,
            sqlite3.Error,
        ) as exc:
            failed = True
            if isinstance(exc, JQuantsRateLimitError):
                jquants_rate_limited = True
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    print("bootstrap-cache incomplete" if failed else "bootstrap-cache done", file=out, flush=True)
    return 1 if failed else 0


def _bootstrap_market_cache(
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    out: TextIO,
) -> None:
    refresh = _maintenance(providers)
    bars_start = asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS)
    normalized_start = asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS)
    shared_bars_start = min(bars_start, normalized_start)
    print("bootstrap-cache jquants eq_master: start", file=out, flush=True)
    master_rows = refresh.ensure_master(asof_date)
    print(
        f"bootstrap-cache jquants eq_master: {master_rows} row(s)",
        file=out,
        flush=True,
    )
    print(
        "bootstrap-cache jquants daily_bars: "
        f"{shared_bars_start.isoformat()}..{asof_date.isoformat()} start",
        file=out,
        flush=True,
    )
    # `ensure_*` rather than `get_*`: the window is over three million rows and
    # the only thing wanted from it here is the count on the next line.
    bar_rows = refresh.ensure_daily_bars(shared_bars_start, asof_date)
    print(
        f"bootstrap-cache jquants daily_bars: {bar_rows} row(s)",
        file=out,
        flush=True,
    )
    calendar_start = asof_date - timedelta(days=_CALENDAR_BOOTSTRAP_BACKWARD_DAYS)
    calendar_end = asof_date + timedelta(days=_CALENDAR_BOOTSTRAP_FORWARD_DAYS)
    print(
        "bootstrap-cache jquants market_calendar: "
        f"{calendar_start.isoformat()}..{calendar_end.isoformat()} start",
        file=out,
        flush=True,
    )
    calendar_rows = refresh.ensure_market_calendar(calendar_start, calendar_end)
    print(
        f"bootstrap-cache jquants market_calendar: {calendar_rows} row(s)",
        file=out,
        flush=True,
    )


def _bootstrap_financial_cache(
    asof_date: date,
    providers: ProviderBundle,
    _sqlite_path: Path,
    out: TextIO,
) -> None:
    refresh = _maintenance(providers)
    fin_start = asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS)
    normalized_start = asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS)
    shared_fin_start = min(fin_start, normalized_start)
    print(
        "bootstrap-cache jquants fin_summaries: "
        f"{shared_fin_start.isoformat()}..{asof_date.isoformat()} start",
        file=out,
        flush=True,
    )
    # The trailing window is re-read here and nowhere else in the run: the
    # longer normalized-profit window shares this source and must not trigger
    # a second acquisition or a full row load just to report its count.
    summary_rows = refresh.refresh_fin_summaries(
        shared_fin_start,
        asof_date,
        revision_overlap_days=FIN_SUMMARY_REVISION_OVERLAP_DAYS,
        progress=lambda index, total, chunk_start, chunk_end: print(
            "bootstrap-cache jquants fin_summaries chunk "
            f"{index}/{total}: {chunk_start.isoformat()}..{chunk_end.isoformat()}",
            file=out,
            flush=True,
        ),
    )
    print(
        f"bootstrap-cache jquants fin_summaries: {summary_rows} row(s)",
        file=out,
        flush=True,
    )


def _bootstrap_jpx_cache(
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    out: TextIO,
) -> None:
    print("bootstrap-cache jpx snapshots: start", file=out, flush=True)
    if providers.jpx_refresh is None:
        raise JPXProviderError("JPX maintenance provider is not configured")
    jpx_result = providers.jpx_refresh.bootstrap_cache(asof_date)
    print(
        "bootstrap-cache jpx snapshots: "
        + ", ".join(f"{key}={value}" for key, value in sorted(jpx_result.items())),
        file=out,
        flush=True,
    )


def _bootstrap_jpx_earnings(
    asof_date: date, providers: ProviderBundle, _path: Path, out: TextIO
) -> None:
    if providers.jpx_refresh is None:
        raise JPXProviderError("JPX maintenance provider is not configured")
    result = providers.jpx_refresh.refresh_earnings_calendar_snapshot(asof_date)
    print(f"JPX earnings_calendar: {result.valid_record_count} row(s)", file=out, flush=True)


def _bootstrap_jpx_regulations(
    asof_date: date, providers: ProviderBundle, _path: Path, out: TextIO
) -> None:
    if providers.jpx_refresh is None:
        raise JPXProviderError("JPX maintenance provider is not configured")
    result = providers.jpx_refresh.refresh_regulation_snapshot(asof_date)
    print(f"JPX regulations: {len(result.flags_by_ticker)} ticker(s)", file=out, flush=True)
