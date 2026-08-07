"""Cache maintenance commands: bootstrap, EDINET extraction, coverage checks."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from baibai_engine.market.sqlite import (
    EmptyRangeReplacementError,
    SQLiteSchemaError,
    count_overlapping_source_coverage,
    count_source_coverage,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    open_connection,
    source_coverage_sources,
)
from baibai_engine.screening.buyback_store import refresh_buyback_reports
from baibai_engine.screening.metrics import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)
from baibai_engine.screening.providers.edinet import (
    EDINETProviderError,
)
from baibai_engine.screening.providers.jpx import JPXProviderError
from baibai_engine.screening.providers.jquants import (
    JQuantsProviderError,
)
from baibai_engine.screening.sqlite_coverage import (
    CacheCoverageIssue,
    verify_screening_sqlite_coverage,
)
from baibai_engine.screening.sqlite_reader import (
    read_eq_master_exact,
    weekly_margin_candidate_dates,
)

from .providers import ProviderBundle

# Market calendar bootstrap window. The unattended daily batch reads the current
# day's calendar row to gate on business days, so bootstrap fetches a forward
# window that always contains the run date and the next several weeks. Future
# rows are harmless to every calendar reader (all use point/range queries) and
# coverage verification only requires the as-of row itself.
_CALENDAR_BOOTSTRAP_BACKWARD_DAYS = 7
_CALENDAR_BOOTSTRAP_FORWARD_DAYS = 45


def verify_cache_coverage_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    required_jpx_sources: Iterable[str] = (),
    allow_stale_jpx: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """Check whether SQLite can serve every source `screening run` will read.

    This is intentionally local-only: it does not inspect raw JSON files and
    does not call provider APIs. Refresh/rebuild must happen before this check.
    """
    out = stdout if stdout is not None else sys.stdout
    issues = verify_screening_sqlite_coverage(
        sqlite_path,
        asof_date,
        required_jpx_sources=required_jpx_sources,
        allow_stale_jpx=allow_stale_jpx,
    )
    if issues:
        _print_cache_coverage_issues(issues, asof_date=asof_date, stream=out)
        return 1
    print(
        f"SQLite cache coverage complete for --asof {asof_date.isoformat()}: {sqlite_path}",
        file=out,
    )
    return 0


def _print_cache_coverage_issues(
    issues: Sequence[CacheCoverageIssue],
    *,
    asof_date: date,
    stream: TextIO,
) -> None:
    print(f"SQLite cache coverage incomplete for --asof {asof_date.isoformat()}:", file=stream)
    for issue in issues:
        print(f"  {issue.source} {issue.requirement}: {issue.reason}", file=stream)
    print(
        "screening run is cache-only and will not fall back to raw JSON or provider APIs; "
        "run bootstrap-cache --asof and extract-edinet-metrics, then rerun coverage verification.",
        file=stream,
    )


def invalidate_coverage_command(
    *,
    sqlite_path: Path,
    source: str,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Delete a source's coverage rows so the next bootstrap-cache refetches it.

    `source_coverage` gates read-through re-fetching: a complete window is never
    re-fetched. A migration that cannot backfill existing rows needs those rows
    re-pulled, so this removes the coverage bookkeeping (the cache is rebuildable)
    and the next `bootstrap-cache --asof` repopulates it. With `--start`/`--end`
    only windows overlapping that range are removed; without them the whole source
    is invalidated. The target row count is printed before the delete; no confirm
    prompt, because the cache can always be rebuilt.
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
        "invalidate-coverage done; run `bootstrap-cache --asof` to refetch the source",
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
    reach back far enough". Covering years that way costs one 1200-day and one 730-day
    re-fetch per as-of; naming the window once costs one pass, and the coverage merge
    joins it to what is already held.

    Each source is fetched independently and reported on its own line, because a
    provider that cannot answer for one of them says nothing about the others. Chunks
    already covered are skipped, so a run interrupted after hours resumes where it
    stopped rather than starting over.

    Bars come first: the month-end grid that ``backfill-master`` fills is derived from
    the bar store, so snapshots for months without bars cannot be requested yet.

    The store this writes to is named on the first line, and whether it already held
    anything. The path comes from the working directory, so a run started from the
    wrong one otherwise spends hours filling a store nobody reads and reports success.
    """
    out = stdout if stdout is not None else sys.stdout
    window = f"{start.isoformat()}..{end.isoformat()}"
    state = "existing" if sqlite_path.exists() else "new"
    print(f"backfill-history store: {sqlite_path} ({state})", file=out, flush=True)
    print(f"backfill-history start: {window}", file=out, flush=True)
    sources: tuple[tuple[str, Callable[[date, date], Sequence[object]], bool], ...] = (
        ("daily_bars", providers.jquants.get_eq_bars_daily_range, True),
        ("fin_summaries", providers.jquants.get_fin_summary_range, True),
        ("market_calendar", providers.jquants.get_mkt_calendar, False),
    )
    weekly_margin_source = "weekly_margin"
    failures: list[str] = []
    for name, fetch, by_year in sources:
        print(f"backfill-history {name}: {window} start", file=out, flush=True)
        # The range readers answer with every row in the window, so asking for a
        # decade at once holds a decade of bars in memory for the sake of a count.
        # A year at a time bounds that and reports progress on a pass that runs for
        # hours; the calendar is one provider call and is not worth splitting.
        spans = _calendar_year_spans(start, end) if by_year else ((start, end),)
        count = 0
        try:
            for span_start, span_end in spans:
                count += len(fetch(span_start, span_end))
        except (
            JQuantsProviderError,
            SQLiteSchemaError,
            EmptyRangeReplacementError,
            sqlite3.Error,
        ) as exc:
            print(
                f"backfill-history {name}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            failures.append(name)
            continue
        print(f"backfill-history {name}: {count} row(s)", file=out, flush=True)
    print(f"backfill-history {weekly_margin_source}: {window} start", file=out, flush=True)
    try:
        weeks = weekly_margin_candidate_dates(sqlite_path, start, end)
        if not weeks:
            # A window with no candidate week fetched nothing. Reporting that as
            # success is how a backfill silently leaves a hole, so it is a failure
            # of this source rather than a quiet zero.
            raise JQuantsProviderError(
                f"no weekly margin balance date candidates in {window}; "
                "the window holds no complete week of stored trading days"
            )
        margin_rows = sum(
            len(providers.jquants.get_mkt_margin_interest_week(week)) for week in weeks
        )
    except (
        JQuantsProviderError,
        SQLiteSchemaError,
        EmptyRangeReplacementError,
        sqlite3.Error,
    ) as exc:
        print(
            f"backfill-history {weekly_margin_source}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        failures.append(weekly_margin_source)
    else:
        print(
            f"backfill-history {weekly_margin_source}: {len(weeks)} week(s), {margin_rows} row(s)",
            file=out,
            flush=True,
        )
    if failures:
        print(
            f"backfill-history: {len(failures)} source(s) failed: {', '.join(failures)}",
            file=sys.stderr,
        )
        return 1
    print(f"backfill-history done: {window}", file=out, flush=True)
    return 0


# The delta axis reaches back 26 balance dates, so an incremental bootstrap has to
# hold at least that many weeks. The slack absorbs the weeks the exchange skips.
_WEEKLY_MARGIN_BOOTSTRAP_DAYS = 230

FIN_SUMMARY_REVISION_OVERLAP_DAYS = 7
"""How far back a bootstrap re-reads financial summaries it already has.

A correction is disclosed on its own date and arrives as a new row, so past days
do not need re-reading for that. What this window catches is a filing the provider
publishes for a date the batch has already fetched. One week spans a full
disclosure cadence including a weekend and a closure.

The store's own coverage is what decides how far back a *gap* is fetched, so this
window is never the thing that recovers a stopped batch — using it that way would
silently miss everything older than seven days the moment an outage ran longer.
"""

BACKFILL_MASTER_CONSECUTIVE_FAILURE_LIMIT = 3
"""連続失敗で打ち切る本数。

per-date の失敗（その日の断面が無い）は残りの日付と独立だが、systemic な失敗
（認証・plan 外・network 断）は全日付で同じように失敗する。区別せず続けると、
非 retryable な認証エラーでもグリッド全日付ぶんの request を投げ切ってしまう。
"""


def backfill_master_command(
    *,
    asof_dates: Sequence[date],
    providers: ProviderBundle,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Store the point-in-time security master for each requested date only.

    A calibration cohort becomes production evidence only when its population
    comes from a master snapshot of its own date. ``bootstrap-cache`` reaches
    that state as a side effect of also re-fetching a 1200-day bar window and a
    730-day summary window, which costs hours per date; the snapshot itself is
    one request. Backfilling a monthly grid is therefore one cheap pass here
    instead of one expensive pass per cohort.

    Each date is independent: a date the provider cannot answer is reported and
    the pass continues, and the exit code is non-zero if any date failed. A date
    whose exact snapshot is already stored is served from the cache and prints
    ``cached``, so re-running a pass costs nothing; replacing a stored snapshot
    requires ``invalidate-coverage --source jquants_master_snapshots`` first.
    Enough consecutive failures end the pass, because a systemic failure fails
    every remaining date the same way.
    """
    out = stdout if stdout is not None else sys.stdout
    print(f"backfill-master start: {len(asof_dates)} date(s)", file=out, flush=True)
    failures: list[date] = []
    consecutive = 0
    for index, asof_date in enumerate(asof_dates):
        iso = asof_date.isoformat()
        cached = read_eq_master_exact(sqlite_path, asof_date) is not None
        try:
            securities = providers.jquants.get_eq_master(asof_date)
        except (JQuantsProviderError, SQLiteSchemaError, sqlite3.Error) as exc:
            print(f"backfill-master {iso}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append(asof_date)
            consecutive += 1
            if consecutive >= BACKFILL_MASTER_CONSECUTIVE_FAILURE_LIMIT:
                remaining = len(asof_dates) - index - 1
                print(
                    f"backfill-master: {consecutive} consecutive failures; "
                    f"stopping with {remaining} date(s) not attempted",
                    file=sys.stderr,
                )
                break
            continue
        consecutive = 0
        source = "cached" if cached else "fetched"
        print(f"backfill-master {iso}: {source} {len(securities)} row(s)", file=out, flush=True)
    if failures:
        print(
            f"backfill-master: {len(failures)} of {len(asof_dates)} date(s) failed",
            file=sys.stderr,
        )
        return 1
    print("backfill-master done", file=out, flush=True)
    return 0


def bootstrap_cache_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    bars_start = asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS)
    fin_start = asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS)
    normalized_start = asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS)
    try:
        print(f"bootstrap-cache start: asof={asof_date.isoformat()}", file=out, flush=True)
        print("bootstrap-cache jquants eq_master: start", file=out, flush=True)
        securities = providers.jquants.get_eq_master(asof_date)
        print(
            f"bootstrap-cache jquants eq_master: {len(securities)} row(s)",
            file=out,
            flush=True,
        )
        print(
            "bootstrap-cache jquants daily_bars: "
            f"{bars_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        # `ensure_*` rather than `get_*`: the window is over three million rows and
        # the only thing wanted from it here is the count on the next line.
        bar_rows = providers.jquants.ensure_eq_bars_daily_range(bars_start, asof_date)
        print(
            f"bootstrap-cache jquants daily_bars: {bar_rows} row(s)",
            file=out,
            flush=True,
        )
        print(
            "bootstrap-cache jquants split_normalization_bars: "
            f"{normalized_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        split_bars = providers.jquants.get_adjustment_factor_bars_range(normalized_start, asof_date)
        print(
            f"bootstrap-cache jquants split_normalization_bars: {len(split_bars)} row(s)",
            file=out,
            flush=True,
        )
        print(
            "bootstrap-cache jquants fin_summaries: "
            f"{fin_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        # The trailing window is re-read here and nowhere else in the run: the
        # normalized-profit call below shares this source, and asking it there too
        # would buy the same week twice.
        summary_rows = providers.jquants.refresh_fin_summary_range(
            fin_start,
            asof_date,
            revision_overlap_days=FIN_SUMMARY_REVISION_OVERLAP_DAYS,
        )
        print(
            f"bootstrap-cache jquants fin_summaries: {summary_rows} row(s)",
            file=out,
            flush=True,
        )
        print(
            "bootstrap-cache jquants normalized_profit_fy_summaries: "
            f"{normalized_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        fy_summaries = providers.jquants.get_fy_summary_range(normalized_start, asof_date)
        print(
            f"bootstrap-cache jquants normalized_profit_fy_summaries: {len(fy_summaries)} row(s)",
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
        calendar = providers.jquants.get_mkt_calendar(calendar_start, calendar_end)
        print(
            f"bootstrap-cache jquants market_calendar: {len(calendar)} row(s)",
            file=out,
            flush=True,
        )
        # Balance dates come from the stored trading calendar, so the bars fetch
        # above has to have happened first; on a store with no bars this proposes
        # nothing and the source stays empty rather than guessing Fridays.
        margin_weeks = weekly_margin_candidate_dates(
            sqlite_path,
            asof_date - timedelta(days=_WEEKLY_MARGIN_BOOTSTRAP_DAYS),
            asof_date,
        )
        print(
            f"bootstrap-cache jquants weekly_margin: {len(margin_weeks)} week(s) start",
            file=out,
            flush=True,
        )
        margin_rows = 0
        for week_end in margin_weeks:
            margin_rows += len(providers.jquants.get_mkt_margin_interest_week(week_end))
        print(
            f"bootstrap-cache jquants weekly_margin: {margin_rows} row(s)",
            file=out,
            flush=True,
        )
        if providers.edinet is not None:
            print(
                "bootstrap-cache edinet documents: "
                f"{fin_start.isoformat()}..{asof_date.isoformat()} start",
                file=out,
                flush=True,
            )
            edinet_result = providers.edinet.bootstrap_cache(fin_start, asof_date)
            print(
                "bootstrap-cache edinet documents: "
                + ", ".join(f"{key}={value}" for key, value in sorted(edinet_result.items())),
                file=out,
                flush=True,
            )
        else:
            print(
                "note: EDINET provider is not configured; skipping EDINET bootstrap",
                file=sys.stderr,
            )
        print("bootstrap-cache jpx snapshots: start", file=out, flush=True)
        jpx_result = providers.jpx.bootstrap_cache(asof_date)
        print(
            "bootstrap-cache jpx snapshots: "
            + ", ".join(f"{key}={value}" for key, value in sorted(jpx_result.items())),
            file=out,
            flush=True,
        )
    except (
        JQuantsProviderError,
        EDINETProviderError,
        JPXProviderError,
        EmptyRangeReplacementError,
        sqlite3.Error,
    ) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("bootstrap-cache done", file=out, flush=True)
    return 0


def refresh_edinet_documents_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    stdout: TextIO | None = None,
) -> int:
    """Refresh current EDINET list state independently of broad cache coverage."""
    out = stdout if stdout is not None else sys.stdout
    if providers.edinet is None:
        print("EDINET provider is not configured", file=sys.stderr)
        return 1
    try:
        result = providers.edinet.refresh_document_state(asof_date)
    except (EDINETProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "refresh-edinet-documents: "
        + ", ".join(f"{key}={value}" for key, value in sorted(result.items())),
        file=out,
        flush=True,
    )
    return 0


def refresh_buyback_reports_command(
    *,
    asof_date: date,
    lookback_days: int,
    providers: ProviderBundle,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Read the buyback authorization state out of the form-220 filings already listed.

    Separate from `extract-edinet-metrics` because it reads a different form into a
    different table, and because keeping it out of that command's module keeps it out of
    the extractor's revision manifest.
    """
    out = stdout if stdout is not None else sys.stdout
    if providers.edinet is None:
        print("EDINET provider is not configured", file=sys.stderr)
        return 1
    since = asof_date - timedelta(days=lookback_days)
    try:
        summary = refresh_buyback_reports(sqlite_path, provider=providers.edinet, since=since)
    except (EDINETProviderError, sqlite3.Error, SQLiteSchemaError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "refresh-buyback-reports: "
        f"since={since.isoformat()} considered={summary.considered} "
        f"stored={summary.stored} unreadable={summary.unreadable} "
        f"without_month_end={summary.without_month_end} "
        f"rate_limited={str(summary.rate_limited).lower()}",
        file=out,
        flush=True,
    )
    # A partial pass is reported, not failed. These values are an annotation: they never
    # enter E[r], ranking, or a gate, so costing the day's publish over a backlog that
    # the next run clears would trade a real output for a cosmetic one.
    return 0
