"""Cache maintenance commands: bootstrap, EDINET extraction, coverage checks."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TextIO

from baibai_loop.screening.providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
    select_document_candidates,
)
from baibai_loop.screening.providers.edinet_csv import parse_csv_zip_metric_record
from baibai_loop.screening.providers.jpx import JPXProviderError
from baibai_loop.screening.providers.jquants import (
    JQuantsProviderError,
)
from baibai_loop.screening.sqlite_cache import store_edinet_metrics
from baibai_loop.screening.sqlite_coverage import (
    CacheCoverageIssue,
    verify_screening_sqlite_coverage,
)

from .common import _date_iso
from .providers import EDINETAdapter, ProviderBundle


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
        store_edinet_metrics(
            sqlite_path,
            asof_date,
            [],
            status="failed",
            error=message,
        )
        print(message, file=sys.stderr)
        return 1

    try:
        candidates = select_document_candidates(documents)
    except EDINETProviderError as exc:
        message = f"EDINET document selection failed: {type(exc).__name__}: {exc}"
        store_edinet_metrics(
            sqlite_path,
            asof_date,
            [],
            status="failed",
            error=message,
        )
        print(message, file=sys.stderr)
        return 1
    if not candidates:
        message = (
            f"no EDINET filings selected for --asof {asof_date.isoformat()} "
            f"within {start.isoformat()}..{asof_date.isoformat()}"
        )
        store_edinet_metrics(
            sqlite_path,
            asof_date,
            [],
            status="failed",
            error=message,
        )
        print(message, file=sys.stderr)
        return 1
    print(
        f"selected {len(candidates)} EDINET filing(s) from {len(documents)} document(s)",
        file=out,
        flush=True,
    )
    records: list[EdinetMetricRecord] = []
    hard_failure_count = 0
    quality_issue_count = 0
    for candidate in sorted(candidates.values(), key=lambda item: item.ticker):
        parse_failed = False
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
            store_edinet_metrics(
                sqlite_path,
                asof_date,
                [],
                status="failed",
                error=message,
            )
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

    payload = [_metric_record_payload(record) for record in records]
    store_edinet_metrics(
        sqlite_path,
        asof_date,
        payload,
        status="failed" if hard_failure_count else "ok",
        error=f"{hard_failure_count} EDINET CSV hard failures" if hard_failure_count else None,
    )
    print(
        f"wrote {sqlite_path}: {len(records)} EDINET metric records "
        f"from {len(candidates)} selected filings; "
        f"{hard_failure_count} hard failures; "
        f"{quality_issue_count} records with quality issues",
        file=out,
    )
    return 1 if hard_failure_count else 0


def _metric_record_payload(record: EdinetMetricRecord) -> dict[str, object]:
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
    }


def bootstrap_cache_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    bars_start = asof_date - timedelta(days=1200)
    fin_start = asof_date - timedelta(days=730)
    earnings_end = asof_date + timedelta(days=90)
    try:
        print(f"bootstrap-cache start: asof={asof_date.isoformat()}", file=out, flush=True)
        print("bootstrap-cache jquants eq_master: start", file=out, flush=True)
        securities = providers.jquants.get_eq_master()
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
        print(
            "bootstrap-cache jquants earnings_calendar: "
            f"{asof_date.isoformat()}..{earnings_end.isoformat()} start",
            file=out,
            flush=True,
        )
        earnings = providers.jquants.get_eq_earnings_cal(asof_date, earnings_end)
        print(
            f"bootstrap-cache jquants earnings_calendar: {len(earnings)} row(s)",
            file=out,
            flush=True,
        )
        print("bootstrap-cache jquants market_calendar: start", file=out, flush=True)
        calendar = providers.jquants.get_mkt_calendar(asof_date, asof_date)
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
        print("bootstrap-cache jpx regulation: start", file=out, flush=True)
        jpx_result = providers.jpx.bootstrap_cache(asof_date)
        print(
            "bootstrap-cache jpx regulation: "
            + ", ".join(f"{key}={value}" for key, value in sorted(jpx_result.items())),
            file=out,
            flush=True,
        )
    except (JQuantsProviderError, EDINETProviderError, JPXProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("bootstrap-cache done", file=out, flush=True)
    return 0
