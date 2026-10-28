"""source入力の取得・再開・coverage保守。モデル生成や候補選定はしない。"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from baibai_engine.data_collection.edinet.models import EdinetAcquisitionError
from baibai_engine.data_collection.jquants.http import JQuantsAcquisitionError
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.market.input_windows import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_exact,
)
from baibai_jobs.maintenance.source_refresh.jquants import (
    all_issues_daily_margin_backfill_candidate_dates,
    all_issues_daily_margin_candidate_dates,
    final_legacy_week_requires_refresh,
    weekly_margin_candidate_dates,
    weekly_margin_empty_requires_refresh,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_publication import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
)
from baibai_engine.storage.observations.coverage import (
    count_overlapping_source_coverage,
    count_source_coverage,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    source_coverage_sources,
)
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

from baibai_jobs.maintenance.market.providers import JQuantsMaintenance, ProviderBundle


def _maintenance(providers: ProviderBundle) -> JQuantsMaintenance:
    if providers.jquants_refresh is None:
        raise ValueError("this command requires explicit J-Quants maintenance")
    return providers.jquants_refresh


# Incremental calendar acquisition extends through the next several weeks.
# Full historical calendar coverage is prepared separately; this window does not
# establish the long history needed by momentum or bootstrap a missing baseline.
# Readers apply their own date ranges; non-business days do not stop model generation.
_CALENDAR_BOOTSTRAP_BACKWARD_DAYS = 7
_CALENDAR_BOOTSTRAP_FORWARD_DAYS = 45


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
    refresh = _maintenance(providers)
    window = f"{start.isoformat()}..{end.isoformat()}"
    state = "existing" if sqlite_path.exists() else "new"
    print(f"backfill-history store: {sqlite_path} ({state})", file=out, flush=True)
    print(f"backfill-history start: {window}", file=out, flush=True)
    sources: tuple[tuple[str, Callable[[date, date], int], bool], ...] = (
        ("daily_bars", refresh.ensure_daily_bars, True),
        ("fin_summaries", refresh.ensure_fin_summaries, True),
        ("market_calendar", refresh.ensure_market_calendar, False),
        ("short_sale_reports", refresh.ensure_short_sale_reports, True),
        ("margin_alerts", refresh.ensure_margin_alerts, True),
    )
    weekly_margin_source = "weekly_margin"
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
            continue
        print(f"backfill-history {name}: {count} row(s)", file=out, flush=True)
    if start <= LEGACY_WEEKLY_LAST_BALANCE_DATE:
        print(f"backfill-history {weekly_margin_source}: {window} start", file=out, flush=True)
        try:
            weeks = weekly_margin_candidate_dates(sqlite_path, start, end)
            if not weeks:
                raise JQuantsProviderError(
                    f"no weekly margin balance date candidates in {window}; "
                    "the window holds no eligible stored trading days"
                )
            margin_rows = sum(refresh.ensure_weekly_margin(week) for week in weeks)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            SQLiteSchemaError,
            sqlite3.Error,
        ) as exc:
            print(
                f"backfill-history {weekly_margin_source}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            failures.append(weekly_margin_source)
        else:
            print(
                f"backfill-history {weekly_margin_source}: {len(weeks)} week(s), "
                f"{margin_rows} row(s)",
                file=out,
                flush=True,
            )
    daily_margin_source = "all_issues_daily_margin"
    if end >= ALL_ISSUES_DAILY_FIRST_BALANCE_DATE:
        print(f"backfill-history {daily_margin_source}: {window} start", file=out, flush=True)
        try:
            dates = all_issues_daily_margin_backfill_candidate_dates(sqlite_path, start, end)
            daily_margin_rows = sum(refresh.ensure_all_issues_daily_margin(day) for day in dates)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            SQLiteSchemaError,
            sqlite3.Error,
        ) as exc:
            print(
                f"backfill-history {daily_margin_source}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            failures.append(daily_margin_source)
        else:
            print(
                f"backfill-history {daily_margin_source}: {len(dates)} day(s), "
                f"{daily_margin_rows} row(s)",
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
_SHORT_SALE_REPORT_REVISION_OVERLAP_DAYS = 7
_MARGIN_ALERT_REVISION_OVERLAP_DAYS = 7

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
    refresh = _maintenance(providers)
    print(f"backfill-master start: {len(asof_dates)} date(s)", file=out, flush=True)
    failures: list[date] = []
    consecutive = 0
    for index, asof_date in enumerate(asof_dates):
        iso = asof_date.isoformat()
        cached = read_eq_master_exact(sqlite_path, asof_date) is not None
        try:
            row_count = refresh.ensure_master(asof_date)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            SQLiteSchemaError,
            sqlite3.Error,
        ) as exc:
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
        print(f"backfill-master {iso}: {source} {row_count} row(s)", file=out, flush=True)
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
        "edinet-list": _bootstrap_edinet_cache,
        "jpx": _bootstrap_jpx_cache,
        "jpx-earnings": _bootstrap_jpx_earnings,
        "jpx-regulations": _bootstrap_jpx_regulations,
    }
    if source_group != "all" and source_group not in groups:
        raise ValueError("unsupported acquisition source group")
    if source_group == "edinet-list" and providers.edinet_refresh is None:
        print("EDINET acquisition is not configured", file=sys.stderr)
        return 1
    selected = (
        ("jquants-market", "jquants-financial", "edinet-list", "jpx")
        if source_group == "all"
        else (source_group,)
    )
    for group in selected:
        acquire = groups[group]
        try:
            acquire(asof_date, providers, sqlite_path, out)
        except (
            JQuantsAcquisitionError,
            JQuantsProviderError,
            EDINETProviderError,
            EdinetAcquisitionError,
            JPXProviderError,
            sqlite3.Error,
        ) as exc:
            failed = True
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
    # Balance dates come from the stored trading calendar, so the bars fetch
    # above has to have happened first; on a store with no bars this proposes
    # nothing and the source stays empty rather than guessing Fridays.
    margin_weeks = weekly_margin_candidate_dates(
        sqlite_path,
        asof_date - timedelta(days=_WEEKLY_MARGIN_BOOTSTRAP_DAYS),
        asof_date,
        publication_asof=asof_date,
    )
    print(
        f"bootstrap-cache jquants weekly_margin: {len(margin_weeks)} week(s) start",
        file=out,
        flush=True,
    )
    margin_rows = 0
    for week_end in margin_weeks:
        if weekly_margin_empty_requires_refresh(sqlite_path, week_end, asof=asof_date) or (
            week_end == LEGACY_WEEKLY_LAST_BALANCE_DATE
            and asof_date >= LEGACY_WEEKLY_LAST_PUBLICATION_DATE
            and final_legacy_week_requires_refresh(sqlite_path)
        ):
            # An older checkout may have cached an empty response before the
            # final legacy week was published. Re-read this one boundary row
            # after its official publication date so the transition cannot
            # inherit that false-empty claim.
            margin_rows += refresh.refresh_weekly_margin(week_end)
        else:
            margin_rows += refresh.ensure_weekly_margin(week_end)
    print(
        f"bootstrap-cache jquants weekly_margin: {margin_rows} row(s)",
        file=out,
        flush=True,
    )
    alert_start = asof_date - timedelta(days=_MARGIN_ALERT_REVISION_OVERLAP_DAYS)
    print(
        "bootstrap-cache jquants margin_alerts: "
        f"{alert_start.isoformat()}..{asof_date.isoformat()} start",
        file=out,
        flush=True,
    )
    margin_alert_rows = refresh.refresh_margin_alerts(alert_start, asof_date)
    print(
        f"bootstrap-cache jquants margin_alerts: {margin_alert_rows} row(s)",
        file=out,
        flush=True,
    )
    daily_margin_dates = all_issues_daily_margin_candidate_dates(
        sqlite_path, ALL_ISSUES_DAILY_FIRST_BALANCE_DATE, asof_date
    )
    print(
        f"bootstrap-cache jquants all_issues_daily_margin: {len(daily_margin_dates)} day(s) start",
        file=out,
        flush=True,
    )
    daily_margin_rows = sum(
        refresh.ensure_all_issues_daily_margin(balance_date) for balance_date in daily_margin_dates
    )
    print(
        f"bootstrap-cache jquants all_issues_daily_margin: {daily_margin_rows} row(s)",
        file=out,
        flush=True,
    )
    print(
        "bootstrap-cache jquants short_sale_reports: "
        f"{(asof_date - timedelta(days=_SHORT_SALE_REPORT_REVISION_OVERLAP_DAYS)).isoformat()}"
        f"..{asof_date.isoformat()} start",
        file=out,
        flush=True,
    )
    short_report_rows = refresh.refresh_short_sale_reports(
        asof_date - timedelta(days=_SHORT_SALE_REPORT_REVISION_OVERLAP_DAYS), asof_date
    )
    print(
        f"bootstrap-cache jquants short_sale_reports: {short_report_rows} row(s)",
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


def _bootstrap_edinet_cache(
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    out: TextIO,
) -> None:
    fin_start = asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS)
    if providers.edinet_refresh is not None:
        print(
            "bootstrap-cache edinet documents: "
            f"{fin_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        edinet_result = providers.edinet_refresh.bootstrap_cache(fin_start, asof_date)
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


def refresh_edinet_documents_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    stdout: TextIO | None = None,
) -> int:
    """Refresh current EDINET list state independently of broad cache coverage."""
    out = stdout if stdout is not None else sys.stdout
    if providers.edinet_refresh is None:
        print("EDINET provider is not configured", file=sys.stderr)
        return 1
    try:
        result = providers.edinet_refresh.refresh_document_state(asof_date)
    except (EdinetAcquisitionError, EDINETProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "refresh-edinet-documents: "
        + ", ".join(f"{key}={value}" for key, value in sorted(result.items())),
        file=out,
        flush=True,
    )
    return 0
