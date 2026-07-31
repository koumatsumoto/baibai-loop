"""Cache maintenance commands: bootstrap, EDINET extraction, coverage checks."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TextIO

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
from baibai_engine.screening.edinet_revision import (
    compute_extractor_revision,
    has_hard_metric_failure,
)
from baibai_engine.screening.providers.edinet import (
    EdinetDocumentCandidate,
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
    select_document_candidates,
)
from baibai_engine.screening.providers.edinet_csv import parse_csv_zip_metric_record
from baibai_engine.screening.providers.jpx import JPXProviderError
from baibai_engine.screening.providers.jquants import (
    JQuantsProviderError,
)
from baibai_engine.screening.sqlite_cache import store_edinet_metrics
from baibai_engine.screening.sqlite_coverage import (
    CacheCoverageIssue,
    verify_screening_sqlite_coverage,
)
from baibai_engine.screening.sqlite_reader import (
    EDINETMetricBaselineError,
    EDINETMetricBaselineRow,
    read_edinet_metric_baseline,
    read_eq_master_exact,
)
from baibai_engine.screening.store_readiness import unreadable_store_reason

from .common import _date_iso
from .providers import EDINETAdapter, ProviderBundle

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


def extract_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    provider: EDINETAdapter,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    start = asof_date - timedelta(days=lookback_days)
    documents: list[dict[str, Any]] = []
    cursor = start
    listed_days = 0
    print(
        "listing EDINET documents "
        f"{start.isoformat()}..{asof_date.isoformat()} ({lookback_days + 1} day(s))",
        file=out,
        flush=True,
    )
    try:
        while cursor <= asof_date:
            documents.extend(provider.list_documents(cursor))
            cursor += timedelta(days=1)
            listed_days += 1
            if listed_days % 30 == 0 or cursor > asof_date:
                print(
                    f"listed EDINET documents: {listed_days}/{lookback_days + 1} day(s), "
                    f"{len(documents)} document(s)",
                    file=out,
                    flush=True,
                )
    except EDINETProviderError as exc:
        message = f"EDINET document listing failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1

    try:
        candidates = select_document_candidates(documents, origin_start=start)
    except EDINETProviderError as exc:
        message = f"EDINET document selection failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1
    if not candidates:
        message = (
            f"no EDINET filings selected for --asof {asof_date.isoformat()} "
            f"within {start.isoformat()}..{asof_date.isoformat()}"
        )
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1
    print(
        f"selected {len(candidates)} EDINET filing(s) from {len(documents)} document(s)",
        file=out,
        flush=True,
    )
    extractor_revision = compute_extractor_revision()
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except EDINETMetricBaselineError as exc:
        print(f"EDINET metric baseline is corrupt: {exc}", file=sys.stderr)
        return 1
    baseline_asof = baseline.asof_date.isoformat() if baseline is not None else "none"
    full_rebuild_reason = "no_baseline" if baseline is None else "none"
    if (
        baseline is not None
        and baseline.rows
        and all(row.extractor_revision != extractor_revision for row in baseline.rows.values())
    ):
        full_rebuild_reason = "incompatible_revision"
    records: list[EdinetMetricRecord] = []
    reused_count = 0
    downloaded_count = 0
    hard_failure_count = 0
    quality_issue_count = 0
    for candidate in sorted(candidates.values(), key=lambda item: item.ticker):
        baseline_row = baseline.rows.get(candidate.ticker) if baseline is not None else None
        if baseline_row is not None and _can_reuse_edinet_metric(
            candidate=candidate,
            baseline_row=baseline_row,
            extractor_revision=extractor_revision,
        ):
            record = baseline_row.record
            reused_count += 1
            if record.failure_reasons:
                quality_issue_count += 1
            records.append(record)
            continue
        parse_failed = False
        downloaded_count += 1
        try:
            content = provider.download_csv_zip(candidate.doc_id)
            record = parse_csv_zip_metric_record(
                ticker=candidate.ticker,
                doc_id=candidate.doc_id,
                doc_type_code=candidate.doc_type_code,
                content=content,
                submit_datetime=candidate.submit_datetime,
                period_start=candidate.period_start,
                period_end=candidate.period_end,
            )
        except EDINETRateLimitError as exc:
            message = f"EDINET CSV download rate limited: {type(exc).__name__}: {exc}"
            _record_edinet_extraction_failure(sqlite_path, asof_date, message)
            print(message, file=sys.stderr)
            return 1
        except (EDINETProviderError, OSError, ValueError) as exc:
            hard_failure_count += 1
            error_text = str(exc).replace("\n", " ").replace("\r", " ")[:160]
            record = EdinetMetricRecord(
                ticker=candidate.ticker,
                source_doc_id=candidate.doc_id,
                document_type=candidate.doc_type_code,
                source_submit_datetime=candidate.submit_datetime,
                source_period_start=candidate.period_start,
                source_period_end=candidate.period_end,
                failure_reasons=(f"csv_parse_failed:{type(exc).__name__}:{error_text}",),
            )
            parse_failed = True
        if has_hard_metric_failure(record.failure_reasons) and not parse_failed:
            hard_failure_count += 1
            parse_failed = True
        if record.failure_reasons and not parse_failed:
            quality_issue_count += 1
        records.append(record)
        if len(records) % 50 == 0 or len(records) == len(candidates):
            print(
                f"parsed EDINET CSV metrics: {len(records)}/{len(candidates)} filing(s); "
                f"{hard_failure_count} hard failure(s)",
                file=out,
                flush=True,
            )

    if hard_failure_count:
        _record_edinet_extraction_failure(
            sqlite_path,
            asof_date,
            f"{hard_failure_count} EDINET CSV hard failures",
        )
        print(
            "EDINET extraction summary: "
            f"selected={len(candidates)} reused={reused_count} downloaded={downloaded_count} "
            f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
            f"extractor_revision={extractor_revision}; hard_failures={hard_failure_count} "
            f"quality_issues={quality_issue_count}; output=not-written",
            file=out,
        )
        return 1

    source_revisions = {
        candidate.ticker: candidate.source_document_revision for candidate in candidates.values()
    }
    payload = [
        _metric_record_payload(
            record,
            extractor_revision=extractor_revision,
            source_document_revision=source_revisions.get(record.ticker),
        )
        for record in records
    ]
    store_edinet_metrics(
        sqlite_path,
        asof_date,
        payload,
        status="ok",
        error=None,
    )
    print(
        "EDINET extraction summary: "
        f"selected={len(candidates)} reused={reused_count} downloaded={downloaded_count} "
        f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
        f"extractor_revision={extractor_revision}; hard_failures={hard_failure_count} "
        f"quality_issues={quality_issue_count}; output={sqlite_path}",
        file=out,
    )
    return 0


def _metric_record_payload(
    record: EdinetMetricRecord,
    *,
    extractor_revision: str,
    source_document_revision: str | None,
) -> dict[str, object]:
    return {
        "ticker": record.ticker,
        "sales_ttm": record.sales_ttm,
        "ocf_ttm": record.ocf_ttm,
        "debt": record.debt,
        "cash": record.cash,
        "ebitda_ttm": record.ebitda_ttm,
        "consolidation_basis": record.consolidation_basis,
        "ttm_quality_ev_ebitda": record.ttm_quality_ev_ebitda.value,
        "ttm_quality_p_s": record.ttm_quality_p_s.value,
        "ttm_quality_pcfr": record.ttm_quality_pcfr.value,
        "operating_profit_ttm": record.operating_profit_ttm,
        "depreciation_and_amortization_ttm": record.depreciation_and_amortization_ttm,
        "capex_ttm": record.capex_ttm,
        "fcf_ttm": record.fcf_ttm,
        "net_cash": record.net_cash,
        "equity": record.equity,
        "total_assets": record.total_assets,
        "ttm_quality_fcf": record.ttm_quality_fcf.value,
        "ttm_quality_net_cash": record.ttm_quality_net_cash.value,
        "source_doc_id": record.source_doc_id,
        "document_type": record.document_type,
        "source_submit_datetime": record.source_submit_datetime,
        "source_period_start": _date_iso(record.source_period_start),
        "source_period_end": _date_iso(record.source_period_end),
        "capex_source": record.capex_source,
        "failure_reasons": list(record.failure_reasons),
        "extractor_revision": extractor_revision,
        "source_document_revision": source_document_revision,
    }


def _can_reuse_edinet_metric(
    *,
    candidate: EdinetDocumentCandidate,
    baseline_row: EDINETMetricBaselineRow,
    extractor_revision: str,
) -> bool:
    record = baseline_row.record
    return (
        baseline_row.extractor_revision == extractor_revision
        and baseline_row.source_document_revision == candidate.source_document_revision
        and candidate.source_document_revision is not None
        and record.ticker == candidate.ticker
        and record.source_doc_id == candidate.doc_id
        and record.document_type == candidate.doc_type_code
        and record.source_submit_datetime == candidate.submit_datetime
        and record.source_period_start == candidate.period_start
        and record.source_period_end == candidate.period_end
    )


def _record_edinet_extraction_failure(
    sqlite_path: Path,
    asof_date: date,
    message: str,
) -> None:
    """Record a failed attempt without destroying a usable target-day snapshot.

    Recording the failure deletes the day's rows, so the check for an existing
    snapshot is what stands between a failed attempt and the loss of a good one.
    A store that exists but the readers cannot open answers "no snapshot" to that
    check while the write path below would migrate it and delete anyway, so there
    the question went unanswered and nothing is written. A store that does not
    exist yet holds nothing to lose, and the failure is recorded as usual.
    """
    if sqlite_path.exists() and unreadable_store_reason(sqlite_path) is not None:
        return
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except EDINETMetricBaselineError:
        return
    if baseline is not None and baseline.asof_date == asof_date:
        return
    store_edinet_metrics(
        sqlite_path,
        asof_date,
        [],
        status="failed",
        error=message,
    )


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
    sqlite_path: Path | None = None,
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
    if sqlite_path is not None:
        state = "existing" if sqlite_path.exists() else "new"
        print(f"backfill-history store: {sqlite_path} ({state})", file=out, flush=True)
    print(f"backfill-history start: {window}", file=out, flush=True)
    sources: tuple[tuple[str, Callable[[date, date], Sequence[object]], bool], ...] = (
        ("daily_bars", providers.jquants.get_eq_bars_daily_range, True),
        ("fin_summaries", providers.jquants.get_fin_summary_range, True),
        ("market_calendar", providers.jquants.get_mkt_calendar, False),
    )
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
    if failures:
        print(
            f"backfill-history: {len(failures)} source(s) failed: {', '.join(failures)}",
            file=sys.stderr,
        )
        return 1
    print(f"backfill-history done: {window}", file=out, flush=True)
    return 0


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
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    bars_start = asof_date - timedelta(days=1200)
    fin_start = asof_date - timedelta(days=730)
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
        bars = providers.jquants.get_eq_bars_daily_range(bars_start, asof_date)
        print(
            f"bootstrap-cache jquants daily_bars: {len(bars)} row(s)",
            file=out,
            flush=True,
        )
        print(
            "bootstrap-cache jquants fin_summaries: "
            f"{fin_start.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        summaries = providers.jquants.get_fin_summary_range(fin_start, asof_date)
        print(
            f"bootstrap-cache jquants fin_summaries: {len(summaries)} row(s)",
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
