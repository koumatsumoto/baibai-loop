from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from baibai_loop._env import load_project_env

from .config import (
    DEFAULT_SQLITE_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from .date_utils import weekday_distance
from .filesystem import write_text_atomic
from .freshness import detect_edinet_freshness_warnings, load_disclosure_events
from .lineage import (
    build_provider_settings,
    build_run_id,
    compute_config_hash,
    compute_sqlite_fingerprint,
)
from .metrics import (
    build_metrics,
    build_shares_outstanding_index,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from .providers import EDINETProvider, JPXProvider, JQuantsProvider
from .providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
    parse_csv_zip_metric_record,
    select_document_candidates,
)
from .providers.jpx import JPXProviderError, JPXRegulationSnapshot
from .providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
    JQuantsProviderError,
)
from .render import JST, build_output_path, render_screened_yaml
from .rule_config import (
    DEFAULT_RULES_PATH,
    CashflowYieldLane,
    FcfYieldLane,
    SalesDiscountGrowthLane,
    ScreeningRules,
    load_screening_rules,
)
from .rules import evaluate_screening
from .schema import (
    FinancialSnapshot,
    ScreenedCandidate,
    ScreenedRunDocument,
    SecurityMaster,
    TTMQuality,
    normalize_ticker,
)
from .sqlite_cache import SQLiteCacheError, rebuild_from_raw, store_edinet_metrics
from .sqlite_coverage import CacheCoverageIssue, verify_screening_sqlite_coverage
from .tiers import position_tier
from .universe import (
    build_universe,
)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


class JQuantsAdapter(Protocol):
    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]: ...

    def get_eq_master(self) -> list[SecurityMaster]: ...

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]: ...

    def get_fin_summary_range(
        self,
        start: date,
        end: date,
    ) -> list[JQuantsFinancialSummary]: ...

    def get_eq_earnings_cal(self, start: date, end: date) -> list[dict[str, object]]: ...

    def bootstrap_cache(self, start: date, end: date) -> Mapping[str, int]: ...


class EDINETAdapter(Protocol):
    def load_metric_records(self, asof_date: date) -> Mapping[str, EdinetMetricRecord]: ...

    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def bootstrap_cache(self, start: date, end: date) -> Mapping[str, int]: ...


class JPXAdapter(Protocol):
    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot: ...

    def has_regulation_cache(self, asof_date: date) -> bool: ...

    def bootstrap_cache(self, asof_date: date) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    jquants: JQuantsAdapter
    edinet: EDINETAdapter | None
    jpx: JPXAdapter


class _ScreenedCandidateInput(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    ticker: str
    name: str | None = None
    sector_33: str = ""
    market_cap_oku: int | float | None = None
    evidence_hits: list[dict[str, object]] = Field(default_factory=list)
    metrics: dict[str, object] = Field(default_factory=dict)
    metrics_breakdown: dict[str, object] = Field(default_factory=dict)
    freshness_warnings: list[dict[str, object]] = Field(default_factory=list)
    next_earnings_date: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


class _ScreenedFrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    candidates: list[_ScreenedCandidateInput] = Field(default_factory=list)


class _OutlookJudgement(BaseModel):
    model_config = ConfigDict(frozen=True, strict=False, extra="allow")

    status: str | None = None


class _OutlookFrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, strict=False, extra="allow")

    sectors: Mapping[str, _OutlookJudgement] = Field(default_factory=dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m baibai_loop.screening.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run weekly screening")
    run_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    run_parser.add_argument(
        "--allow-stale-jpx",
        action="store_true",
        help="allow an already-cached stale JPX snapshot for a historical backfill asof",
    )
    run_parser.add_argument(
        "--output-path",
        help=(
            "write candidates YAML to this path instead of the canonical "
            "records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml path"
        ),
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing candidates YAML output path",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap canonical SQLite inputs for a screening as-of date",
    )
    bootstrap_parser.add_argument(
        "--asof",
        help="screening target date (YYYY-MM-DD); computes each source window automatically",
    )
    bootstrap_parser.add_argument("--start", help="legacy explicit start date (YYYY-MM-DD)")
    bootstrap_parser.add_argument("--end", help="legacy explicit end date (YYYY-MM-DD)")

    extract_parser = subparsers.add_parser(
        "extract-edinet-metrics",
        help="extract EDINET type=5 CSV metrics into canonical SQLite",
    )
    extract_parser.add_argument("--asof", required=True, help="metrics as-of date (YYYY-MM-DD)")
    extract_parser.add_argument(
        "--lookback-days",
        type=int,
        default=540,
        help="EDINET document-list lookback window in calendar days (default 540)",
    )

    rebuild_parser = subparsers.add_parser(
        "rebuild-cache",
        help=("legacy migration: rebuild canonical SQLite under data/screening/ from raw JSON"),
    )
    rebuild_parser.add_argument(
        "--raw-dir",
        required=True,
        help="legacy raw JSON root to import (required; disposable .cache is not a default)",
    )
    rebuild_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"output SQLite path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    coverage_parser = subparsers.add_parser(
        "verify-cache-coverage",
        help="check that SQLite can serve all local inputs required by screening run",
    )
    coverage_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    coverage_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    coverage_parser.add_argument(
        "--require-edinet-metrics",
        action="store_true",
        dest="require_edinet_metrics",
        help="require EDINET metrics coverage for --asof (default)",
    )
    coverage_parser.add_argument(
        "--allow-missing-edinet-metrics",
        action="store_false",
        dest="require_edinet_metrics",
        help="legacy/degraded verification only; run still requires EDINET metrics",
    )
    coverage_parser.set_defaults(require_edinet_metrics=True)
    coverage_parser.add_argument(
        "--rules-path",
        default=str(DEFAULT_RULES_PATH),
        help=f"screening rules path for required JPX sources (default: {DEFAULT_RULES_PATH})",
    )
    coverage_parser.add_argument(
        "--allow-stale-jpx",
        action="store_true",
        help="degraded verification only; allow old JPX fetched_at_utc snapshots",
    )

    select_parser = subparsers.add_parser(
        "select",
        help="rank research candidates by combining candidates with outlook sectors",
    )
    select_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    select_parser.add_argument(
        "--outlook",
        help=(
            "outlook path to apply (default: latest "
            "records/03-outlook/<YYYY>/<MM>/outlook-*.yaml on or before asof)"
        ),
    )
    select_parser.add_argument(
        "--candidates",
        help=(
            "candidates YAML path to rank (default: "
            "records/04-candidates/<YYYY>/<MM>/<YYYY-MM-DD>.yaml)"
        ),
    )
    select_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="maximum number of candidates to emit (default 10)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "select":
        # select reads existing candidates YAML, outlook markdown, and the
        # local rule config for output parameters; no provider credentials are
        # needed.
        return select_command(
            asof_date=_parse_iso_date(args.asof),
            candidates_path=Path(args.candidates) if args.candidates else None,
            outlook_path=Path(args.outlook) if args.outlook else None,
            top=args.top,
        )

    if args.command == "rebuild-cache":
        # rebuild-cache reads local raw JSON and writes a SQLite file; no API tokens needed.
        return rebuild_cache_command(
            raw_dir=Path(args.raw_dir),
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "verify-cache-coverage":
        # verify-cache-coverage is local-only and never reads raw JSON or calls providers.
        rules = load_screening_rules(Path(args.rules_path))
        return verify_cache_coverage_command(
            sqlite_path=Path(args.sqlite_path),
            asof_date=_parse_iso_date(args.asof),
            require_edinet_metrics=args.require_edinet_metrics,
            required_jpx_sources=rules.universe.required_jpx_flags,
            allow_stale_jpx=args.allow_stale_jpx,
        )

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sqlite_path = config.sqlite_cache_dir / "market.sqlite"
    run_asof_date = _parse_iso_date(args.asof) if args.command == "run" else None
    run_rules = load_screening_rules(config.rules_path) if args.command == "run" else None
    if args.command == "run":
        assert run_asof_date is not None
        coverage_issues = verify_screening_sqlite_coverage(
            sqlite_path,
            run_asof_date,
            require_edinet_metrics=True,
            required_jpx_sources=run_rules.universe.required_jpx_flags if run_rules else (),
            allow_stale_jpx=args.allow_stale_jpx,
        )
        if coverage_issues:
            _print_cache_coverage_issues(
                coverage_issues, asof_date=run_asof_date, stream=sys.stderr
            )
            return 1
    providers = ProviderBundle(
        jquants=JQuantsProvider(
            config.jquants_refresh_token,
            config.cache_dir,
            sqlite_path=sqlite_path,
            cache_only=args.command == "run",
        ),
        edinet=(
            EDINETProvider(
                config.edinet_api_key,
                config.cache_dir,
                sqlite_path=sqlite_path,
                cache_only=args.command == "run",
            )
            if config.edinet_api_key or args.command == "run"
            else None
        ),
        jpx=JPXProvider(
            config.cache_dir,
            regulation_urls=config.jpx_regulation_urls,
            special_caution_index_url=config.jpx_special_caution_index_url,
            sqlite_path=sqlite_path,
            cache_only=args.command == "run",
        ),
    )

    if args.command == "run":
        assert run_asof_date is not None
        return run_command(
            run_asof_date,
            config,
            providers,
            rules=run_rules,
            allow_stale_jpx=args.allow_stale_jpx,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
        )

    if args.command == "bootstrap-cache":
        if args.asof:
            if args.start or args.end:
                print("--asof cannot be combined with --start/--end", file=sys.stderr)
                return 1
            return bootstrap_cache_command(
                asof_date=_parse_iso_date(args.asof),
                providers=providers,
            )
        if not args.start or not args.end:
            print("bootstrap-cache requires --asof, or both --start and --end", file=sys.stderr)
            return 1
        start = _parse_iso_date(args.start)
        end = _parse_iso_date(args.end)
        if start > end:
            print("--start must be on or before --end", file=sys.stderr)
            return 1
        return bootstrap_cache_command(start=start, end=end, providers=providers)

    if args.command == "extract-edinet-metrics":
        if args.lookback_days < 0:
            print("--lookback-days must be zero or greater", file=sys.stderr)
            return 1
        if providers.edinet is None:
            print("EDINET_API_KEY is required for extract-edinet-metrics", file=sys.stderr)
            return 1
        return extract_edinet_metrics_command(
            asof_date=_parse_iso_date(args.asof),
            lookback_days=args.lookback_days,
            provider=providers.edinet,
            sqlite_path=sqlite_path,
        )

    raise AssertionError(f"unreachable command: {args.command!r}")


def _parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO date: {raw}") from exc


def run_command(
    asof_date: date,
    config: ScreeningConfig,
    providers: ProviderBundle,
    *,
    rules: ScreeningRules | None = None,
    now: datetime | None = None,
    allow_stale_jpx: bool = False,
    output_path: Path | None = None,
    force: bool = False,
) -> int:
    output_path = output_path or build_output_path(asof_date)
    rules = rules or load_screening_rules(config.rules_path)
    if output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    run_now = now or datetime.now(JST)
    provider_settings = build_provider_settings(config)
    config_hash = compute_config_hash(config, provider_settings)
    run_id = build_run_id(asof_date, config_hash)
    today = run_now.date()
    if (
        not allow_stale_jpx
        and asof_date < today
        and weekday_distance(asof_date, today) > 7
        and not providers.jpx.has_regulation_cache(asof_date)
    ):
        print(
            "JPX regulation cache is missing for a stale backfill; "
            "rerun with --allow-stale-jpx to fetch the latest JPX snapshot explicitly",
            file=sys.stderr,
        )
        return 1

    # bars need 1200d for 3-year self-range percentile and sigma_gap; fin
    # summaries are only consumed for TTM (latest) and prior-year YoY
    # (`_prior_year_summary` in metrics.py), which fits comfortably in 24
    # months of disclosures. Fetching the same 1200d window for both costs
    # ~26 extra fin chunks over J-Quants Light at ~1-3 min each — by far
    # the dominant slowdown when raw cache is sparse.
    bars_start_date = asof_date - timedelta(days=1200)
    fin_start_date = asof_date - timedelta(days=730)
    try:
        calendar_days = providers.jquants.get_mkt_calendar(asof_date, asof_date)
        if not any(day.day == asof_date and day.is_business_day for day in calendar_days):
            print(f"--asof must be a business day: {asof_date.isoformat()}", file=sys.stderr)
            return 1
        securities = providers.jquants.get_eq_master()
        bars = providers.jquants.get_eq_bars_daily_range(bars_start_date, asof_date)
        summaries = providers.jquants.get_fin_summary_range(fin_start_date, asof_date)
        # Pull earnings calendar from asof to asof + 90 calendar days (~ 60
        # business days) so research can populate next_earnings_date and
        # surface kill-switch overlaps at packet build time.
        earnings_records = providers.jquants.get_eq_earnings_cal(
            asof_date, asof_date + timedelta(days=90)
        )
        jpx_snapshot = providers.jpx.get_regulation_snapshot(asof_date)
    except (JQuantsProviderError, JPXProviderError, sqlite3.Error) as exc:
        # 型情報を残して root cause を追いやすくする。secret を含みうる 3rd party
        # exception はラップ済みなので str(exc) 表示で安全。
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    missing_jpx_sources = tuple(
        flag
        for flag in sorted(rules.universe.required_jpx_flags)
        if flag not in jpx_snapshot.source_names
    )
    if missing_jpx_sources:
        print(
            "missing required JPX regulation sources: " + ", ".join(missing_jpx_sources),
            file=sys.stderr,
        )
        return 1

    bars_by_ticker = group_bars_by_ticker(bars)
    summaries_by_ticker = group_summaries_by_ticker(summaries)
    next_earnings_by_ticker = _index_next_earnings(earnings_records, asof_date)
    shares_by_ticker = build_shares_outstanding_index(summaries_by_ticker)
    edinet_load_error: str | None = None
    edinet_by_ticker: Mapping[str, EdinetMetricRecord] = {}
    if providers.edinet is None:
        print("EDINET preprocessed metrics provider is required for screening run", file=sys.stderr)
        return 1
    try:
        edinet_by_ticker = providers.edinet.load_metric_records(asof_date)
    except (EDINETProviderError, OSError, ValueError) as exc:
        # cache 破損 / JSON 不正 / IO 失敗は EDINET 必須条件を満たせないため、
        # preflight 後の race や run_command 直呼びでも YAML 生成へ進めない。
        edinet_load_error = f"{type(exc).__name__}: {exc}"
        print(f"EDINET load_metric_records failed: {edinet_load_error}", file=sys.stderr)
        return 1
    if not edinet_by_ticker:
        print(
            "EDINET preprocessed metrics are required but empty for screening run", file=sys.stderr
        )
        return 1

    universe_result = build_universe(
        asof_date=asof_date,
        securities=securities,
        bars_by_ticker=bars_by_ticker,
        shares_outstanding_by_ticker=shares_by_ticker,
        jpx_flags_by_ticker=jpx_snapshot.flags_by_ticker,
        min_market_cap_oku=rules.universe.min_market_cap_oku,
        min_avg_turnover_oku=rules.universe.min_avg_turnover_oku,
        listed_under_days=rules.universe.listed_under_days,
        required_jpx_flags=frozenset(rules.universe.required_jpx_flags),
    )
    # is_common_stock フィルタを明示して、同一 4 桁 code に優先株などが混じった場合の
    # dict 上書きを防ぐ (build_universe は非共通株を弾くが、snapshots に残った共通株の
    # SecurityMaster が優先株で上書きされると render の name/sector が誤る)。
    securities_by_ticker = {
        security.code: security
        for security in securities
        if security.is_common_stock and security.code in universe_result.snapshots
    }
    metric_result = build_metrics(
        asof_date=asof_date,
        securities_by_ticker=securities_by_ticker,
        bars_by_ticker=bars_by_ticker,
        summaries_by_ticker=summaries_by_ticker,
        edinet_by_ticker=edinet_by_ticker,
        rules=rules,
    )
    disclosure_load_result = load_disclosure_events(
        config.cache_dir / "disclosures",
        asof_date=asof_date,
    )

    screened_candidates: list[ScreenedCandidate] = []
    fact_lines: list[str] = []
    for ticker in sorted(universe_result.snapshots):
        result = evaluate_screening(
            metric_result.financials[ticker],
            metric_result.derived[ticker],
            rules,
            sector_33=securities_by_ticker[ticker].sector_33,
        )
        if not result.pass_fail:
            continue
        if len(result.evidence_hits) > 1:
            evidence_names = ", ".join(evidence_hit.name for evidence_hit in result.evidence_hits)
            fact_lines.append(f"{ticker}: 複数 evidence_hit hit ({evidence_names})")
        financial = metric_result.financials[ticker]
        derived = metric_result.derived[ticker]
        universe_snapshot = universe_result.snapshots[ticker]
        security = securities_by_ticker[ticker]
        freshness_warnings = detect_edinet_freshness_warnings(
            ticker=ticker,
            financial=financial,
            events_by_ticker=disclosure_load_result.events_by_ticker,
            asof_date=asof_date,
        )
        if freshness_warnings:
            fact_lines.append(
                f"{ticker}: EDINET freshness warning ({len(freshness_warnings)} material events)"
            )
        metrics_breakdown: dict[str, dict[str, float | None]] = {}
        for metric in ("per_trailing", "pbr", "ev_ebitda", "p_s"):
            metrics_breakdown[metric] = {
                "sector_median_gap": derived.sector_median_gap.get(metric),
                "self_range_percentile": derived.self_range_percentile.get(metric),
                "sigma_gap": derived.sigma_gap.get(metric),
            }
        screened_candidates.append(
            ScreenedCandidate(
                ticker=ticker,
                name=security.name,
                per_forward=financial.per_forward,
                per_trailing=financial.per_trailing,
                pbr=financial.pbr,
                ev_ebitda=financial.ev_ebitda,
                p_s=financial.p_s,
                pcfr=financial.pcfr,
                sector_33=security.sector_33,
                evidence_hits=result.evidence_hits,
                ttm_quality={
                    "ev_ebitda": financial.ttm_quality_ev_ebitda,
                    "p_s": financial.ttm_quality_p_s,
                    "pcfr": financial.ttm_quality_pcfr,
                    "ocf_yield": financial.ttm_quality_ocf_yield,
                    "sales": financial.ttm_quality_sales,
                    "fcf_yield": financial.ttm_quality_fcf_yield,
                    "net_cash": financial.ttm_quality_net_cash,
                },
                market_cap_oku=universe_snapshot.market_cap_oku,
                avg_turnover_oku=universe_snapshot.avg_turnover_oku,
                price_change_60d=derived.price_change_60d,
                price_change_4w=derived.ticker_return_4w,
                sector_relative_strength_percentile=derived.sector_relative_strength_percentile,
                metrics={
                    "sales_ttm": financial.sales_ttm,
                    "ocf_ttm": financial.ocf_ttm,
                    "edinet_ocf_ttm": financial.edinet_ocf_ttm,
                    "cash_eq": financial.cash_eq,
                    "total_assets": financial.total_assets,
                    "equity": financial.equity,
                    "cash_to_market_cap": financial.cash_to_market_cap,
                    "price_to_equity": financial.price_to_equity,
                    "equity_ratio": financial.equity_ratio,
                    "ocf_yield": financial.ocf_yield,
                    "net_cash": financial.net_cash,
                    "net_cash_to_market_cap": financial.net_cash_to_market_cap,
                    "debt": financial.debt,
                    "cash": financial.cash,
                    "fcf_ttm": financial.fcf_ttm,
                    "fcf_yield": financial.fcf_yield,
                    "capex_ttm": financial.capex_ttm,
                    "depreciation_and_amortization_ttm": (
                        financial.depreciation_and_amortization_ttm
                    ),
                    "edinet_source_doc_id": financial.edinet_source_doc_id,
                    "edinet_document_type": financial.edinet_document_type,
                    "edinet_source_submit_datetime": financial.edinet_source_submit_datetime,
                    "edinet_source_period_start": _date_iso(financial.edinet_source_period_start),
                    "edinet_source_period_end": _date_iso(financial.edinet_source_period_end),
                    "edinet_capex_source": financial.edinet_capex_source,
                    "edinet_failure_reasons": financial.edinet_failure_reasons,
                    "sales_yoy": financial.sales_yoy,
                    "cfo_yoy": financial.cfo_yoy,
                    "operating_profit": financial.operating_profit,
                    "operating_profit_loss_narrowing": (financial.operating_profit_loss_narrowing),
                    "edinet_freshness_warning_count": len(freshness_warnings),
                },
                metrics_breakdown=metrics_breakdown,
                next_earnings_date=next_earnings_by_ticker.get(ticker),
                split_adjustment_flag=derived.split_adjustment_flag,
                freshness_warnings=freshness_warnings,
            )
        )

    universe_size = len(universe_result.snapshots)
    approx_total = metric_result.ttm_quality_counts.get(
        "approximated", 0
    ) + metric_result.ttm_quality_counts.get("unavailable", 0)
    required_ttm_non_exact = _required_ttm_non_exact_count(metric_result.financials.values(), rules)
    partial_warning = universe_size > 0 and (
        required_ttm_non_exact >= rules.quality.partial_warning_ttm_count
        or (required_ttm_non_exact / universe_size) >= rules.quality.partial_warning_ttm_ratio
        or (metric_result.yoy_missing_count / universe_size)
        >= rules.quality.partial_warning_yoy_missing_ratio
    )
    fallback_lines: list[str] = []
    if approx_total:
        fallback_lines.append(f"ttm_quality 非 exact 件数: {approx_total}")
    if required_ttm_non_exact:
        fallback_lines.append(f"有効 lane 必須 TTM metric 非 exact 件数: {required_ttm_non_exact}")
    if metric_result.yoy_missing_count:
        fallback_lines.append(f"業績悪化フィルタ入力欠損: {metric_result.yoy_missing_count} 銘柄")
    if edinet_load_error is not None:
        fallback_lines.append(f"EDINET 読み込み失敗: {edinet_load_error}")
    if disclosure_load_result.load_errors:
        fallback_lines.append(
            f"Disclosure title scan 読み込み失敗: {len(disclosure_load_result.load_errors)} 件"
        )
    if disclosure_load_result.skipped_record_count:
        fallback_lines.append(
            "Disclosure title scan 必須 key 欠損/不正 record: "
            f"{disclosure_load_result.skipped_record_count} 件"
        )
    if disclosure_load_result.unsupported_record_count:
        fallback_lines.append(
            "Disclosure title scan 未対応 record/layout: "
            f"{disclosure_load_result.unsupported_record_count} 件"
        )

    cache_manifest_hash = compute_sqlite_fingerprint(config.sqlite_cache_dir / "market.sqlite")

    data_sources = ["j-quants-light", "jpx-public-regulation"]
    if edinet_by_ticker:
        data_sources.append("edinet-preprocessed-metrics")
    if disclosure_load_result.file_count:
        data_sources.append("disclosure-title-events")
    provider_status_lines = ["データソース: J-Quants Light（日足・財務サマリー・業績予想）+ JPX"]
    if edinet_by_ticker:
        provider_status_lines.append("EDINET preprocessed metrics: loaded")
    else:
        provider_status_lines.append("EDINET preprocessed metrics: optional unavailable")
    if disclosure_load_result.file_count:
        provider_status_lines.append(
            "Disclosure title material-event scan: "
            f"{disclosure_load_result.event_count} events from "
            f"{disclosure_load_result.file_count} files "
            f"(skipped={disclosure_load_result.skipped_record_count}, "
            f"unsupported={disclosure_load_result.unsupported_record_count}, "
            f"errors={len(disclosure_load_result.load_errors)})"
        )
    else:
        provider_status_lines.append("Disclosure title material-event scan: optional unavailable")

    document = ScreenedRunDocument(
        run_date=asof_date,
        asof_date=asof_date,
        universe_size=universe_size,
        filters={
            "min_market_cap_oku": rules.universe.min_market_cap_oku,
            "min_avg_turnover_oku": rules.universe.min_avg_turnover_oku,
            "exclude_listed_under_days": rules.universe.listed_under_days,
        },
        candidates=tuple(screened_candidates),
        run_at=run_now,
        run_id=run_id,
        data_sources=tuple(data_sources),
        config_hash=config_hash,
        cache_manifest_hash=cache_manifest_hash,
        fact_memo_lines=tuple(fact_lines),
        provider_status_lines=tuple(provider_status_lines),
        universe_exclusion_lines=tuple(
            f"{reason}: {count} 件" for reason, count in universe_result.exclusion_counts.items()
        ),
        ttm_quality_counts=metric_result.ttm_quality_counts,
        evidence_hits_summary=_evidence_hits_summary(screened_candidates, rules),
        fallback_lines=tuple(fallback_lines),
    )
    yaml_text = render_screened_yaml(document)
    write_text_atomic(output_path, yaml_text)
    return 2 if partial_warning else 0


def select_command(
    *,
    asof_date: date,
    outlook_path: Path | None,
    candidates_path: Path | None = None,
    top: int,
    candidates_root: Path | None = None,
    outlook_root: Path | None = None,
    rules: ScreeningRules | None = None,
    stdout: TextIO | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    candidates_root = candidates_root or Path("records/04-candidates")
    outlook_root = outlook_root or Path("records/03-outlook")
    rules = rules or load_screening_rules(_rules_path_from_env())

    candidates_path = candidates_path or (
        candidates_root / f"{asof_date:%Y}" / f"{asof_date:%m}" / f"{asof_date:%Y-%m-%d}.yaml"
    )
    if not candidates_path.exists():
        print(f"candidates file not found: {candidates_path}", file=sys.stderr)
        return 1
    try:
        candidates_fm = TypeAdapter(_ScreenedFrontMatter).validate_python(
            _parse_candidates_yaml_payload(candidates_path)
        )
    except (ValidationError, ValueError) as exc:
        print(f"invalid candidates YAML: {candidates_path}: {exc}", file=sys.stderr)
        return 1

    resolved_outlook_path = outlook_path or _find_latest_outlook(outlook_root, asof_date)
    if resolved_outlook_path is None or not resolved_outlook_path.exists():
        print(
            "outlook file not found. Pass --outlook <path> or create "
            "records/03-outlook/<YYYY>/<MM>/outlook-*.yaml",
            file=sys.stderr,
        )
        return 1
    try:
        outlook_fm = TypeAdapter(_OutlookFrontMatter).validate_python(
            _parse_outlook_yaml(resolved_outlook_path)
        )
    except ValidationError as exc:
        print(f"invalid outlook front matter: {resolved_outlook_path}: {exc}", file=sys.stderr)
        return 1

    sectors_status: Mapping[str, str | None] = {
        sector: judgement.status for sector, judgement in outlook_fm.sectors.items()
    }
    ranked_candidates = _rank_candidates(candidates_fm.candidates, sectors_status)
    lane_toplist_limit = rules.output.lane_toplist_limit
    lane_toplists = _rank_lane_toplists(
        candidates_fm.candidates, sectors_status, lane_toplist_limit
    )
    recommendation_limit = _research_recommendation_limit(
        top=top,
        configured_max=rules.output.research_selection_target_max,
    )
    research_candidates = _recommended_research_candidates(
        lane_toplists=lane_toplists,
        ranked_candidates=ranked_candidates,
        lane_order=rules.output.research_selection_lane_order,
        limit=recommendation_limit,
    )
    summary = {
        "asof": asof_date.isoformat(),
        "candidates_ref": str(candidates_path),
        "outlook_ref": str(resolved_outlook_path),
        "input_count": len(candidates_fm.candidates),
        "after_outlook_filter": len(ranked_candidates),
        "selection_mode": rules.output.selection_mode,
        "research_selection_target_min": rules.output.research_selection_target_min,
        "research_selection_target_max": rules.output.research_selection_target_max,
        "research_selection_lane_order": list(rules.output.research_selection_lane_order),
        "lane_toplist_limit": lane_toplist_limit,
        "lane_toplists": lane_toplists,
        "ranked_candidates": ranked_candidates[:top],
        "candidates": research_candidates,
    }
    yaml.dump(summary, out, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
    return 0


def _rules_path_from_env() -> Path:
    return Path(os.environ.get("SCREENING_RULES_PATH") or DEFAULT_RULES_PATH)


def _parse_outlook_yaml(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"outlook YAML root must be a mapping: {path}")
    return document


def _parse_candidates_yaml_payload(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"candidates YAML root must be a mapping: {path}")
    return payload


def _find_latest_outlook(outlook_root: Path, asof_date: date) -> Path | None:
    if not outlook_root.exists():
        return None
    asof_iso = asof_date.isoformat()
    matches = sorted(outlook_root.glob("*/*/outlook-*.yaml"))
    eligible = [
        path
        for path in matches
        # Heuristic: outlook filename contains a YYYY-MM-DD on or before asof.
        if (m := re.search(r"\d{4}-\d{2}-\d{2}", path.name)) and m.group(0) <= asof_iso
    ]
    return eligible[-1] if eligible else None


def _rank_candidates(
    candidates_input: list[_ScreenedCandidateInput],
    sectors_outlook: Mapping[str, str | None],
) -> list[dict[str, object]]:
    ranked: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for item in candidates_input:
        sector = item.sector_33
        outlook_status = sectors_outlook.get(sector)
        if outlook_status not in {"supportive", "neutral"}:
            continue
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if not eligible_evidence_hits:
            continue
        market_cap = item.market_cap_oku
        independent_evidence_count = len(eligible_evidence_hits)
        selection_lane, selection_metrics, strength_key = _best_selection_evidence(
            eligible_evidence_hits
        )
        sort_key = (
            _macro_rank(outlook_status),
            _lane_rank(selection_lane),
            *strength_key,
            -independent_evidence_count,
            item.ticker,
        )
        candidate: dict[str, object] = {
            "ticker": item.ticker,
            "name": item.name,
            "sector_33": sector,
            "outlook_sector": outlook_status,
            "market_cap_oku": market_cap,
            "evidence_hits": item.evidence_hits,
            "independent_evidence_count": independent_evidence_count,
            "freshness_warnings": item.freshness_warnings,
            "selection_lane": selection_lane,
            "selection_metrics": selection_metrics,
            "next_earnings_date": item.next_earnings_date,
            "position_tier": position_tier(market_cap),
        }
        ranked.append((sort_key, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]


def _rank_lane_toplists(
    candidates_input: list[_ScreenedCandidateInput],
    sectors_outlook: Mapping[str, str | None],
    top: int,
) -> dict[str, list[dict[str, object]]]:
    ranked_by_lane: dict[str, list[tuple[tuple[object, ...], dict[str, object]]]] = {
        "valuation-reversion": [],
        "strict-net-cash-discount": [],
        "fcf-yield-discount": [],
        "cash-rich-asset-discount": [],
        "cashflow-yield-discount": [],
        "sales-discount-growth": [],
    }
    for item in candidates_input:
        sector = item.sector_33
        outlook_status = sectors_outlook.get(sector)
        if outlook_status not in {"supportive", "neutral"}:
            continue
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if not eligible_evidence_hits:
            continue
        for evidence_hit in eligible_evidence_hits:
            name = _string_value(evidence_hit.get("name"))
            if name not in ranked_by_lane:
                continue
            metrics = _metric_map(evidence_hit.get("metrics"))
            sort_key = (
                _macro_rank(outlook_status),
                *_evidence_strength_key(name, metrics),
                -len(eligible_evidence_hits),
                item.ticker,
            )
            ranked_by_lane[name].append(
                (
                    sort_key,
                    _selection_candidate(
                        item,
                        sector=sector,
                        outlook_status=outlook_status,
                        selection_lane=name,
                        selection_metrics=metrics,
                        recommendation_lane=name,
                    ),
                )
            )
    output: dict[str, list[dict[str, object]]] = {}
    for name, entries in ranked_by_lane.items():
        entries.sort(key=lambda item: item[0])
        output[name] = [candidate for _, candidate in entries[:top]]
    return output


def _research_recommendation_limit(*, top: int, configured_max: int) -> int:
    return min(top, configured_max) if configured_max > 0 else top


def _recommended_research_candidates(
    *,
    lane_toplists: Mapping[str, list[dict[str, object]]],
    ranked_candidates: Sequence[dict[str, object]],
    lane_order: Sequence[str],
    limit: int,
) -> list[dict[str, object]]:
    if limit < 1:
        return []

    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()

    for lane in lane_order:
        if len(selected) >= limit:
            break
        for candidate in lane_toplists.get(lane) or []:
            ticker = _string_value(candidate.get("ticker"))
            if ticker is None or ticker in selected_tickers:
                continue
            selected.append(
                _research_recommendation_candidate(
                    candidate,
                    recommendation_lane=lane,
                    lane_order=lane_order,
                )
            )
            selected_tickers.add(ticker)
            break

    for candidate in ranked_candidates:
        if len(selected) >= limit:
            break
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            continue
        selected.append(
            _research_recommendation_candidate(
                candidate,
                recommendation_lane="global-rank",
                lane_order=lane_order,
            )
        )
        selected_tickers.add(ticker)

    return selected


def _research_recommendation_candidate(
    candidate: Mapping[str, object],
    *,
    recommendation_lane: str,
    lane_order: Sequence[str],
) -> dict[str, object]:
    output = dict(candidate)
    output["recommendation_lane"] = recommendation_lane
    selection_lane, selection_metrics = _primary_evidence_by_lane_order(
        output.get("evidence_hits"), lane_order
    )
    if selection_lane is not None:
        output["selection_lane"] = selection_lane
        output["selection_metrics"] = selection_metrics
    return output


def _primary_evidence_by_lane_order(
    raw_evidence_hits: object,
    lane_order: Sequence[str],
) -> tuple[str | None, dict[str, object]]:
    if not isinstance(raw_evidence_hits, Sequence) or isinstance(raw_evidence_hits, str):
        return None, {}
    evidence_by_lane: dict[str, Mapping[str, object]] = {}
    for evidence_hit in raw_evidence_hits:
        if not isinstance(evidence_hit, Mapping):
            continue
        if not _is_sizing_eligible_evidence(evidence_hit):
            continue
        name = _string_value(evidence_hit.get("name"))
        if name is None:
            continue
        evidence_by_lane[name] = evidence_hit
    for lane in lane_order:
        evidence_hit = evidence_by_lane.get(lane)
        if evidence_hit is not None:
            return lane, _metric_map(evidence_hit.get("metrics"))
    return None, {}


def _best_selection_evidence(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[str | None, dict[str, object], tuple[float, ...]]:
    entries = [
        (
            _lane_rank(name),
            _evidence_strength_key(name, metrics),
            name,
            metrics,
        )
        for evidence_hit in evidence_hits
        if (name := _string_value(evidence_hit.get("name"))) is not None
        for metrics in [_metric_map(evidence_hit.get("metrics"))]
    ]
    if not entries:
        return None, {}, (0.0,)
    _, strength_key, name, metrics = min(entries, key=lambda item: (item[0], item[1]))
    return name, metrics, strength_key


def _sizing_eligible_evidence_hits(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(hit for hit in evidence_hits if _is_sizing_eligible_evidence(hit))


def _is_sizing_eligible_evidence(evidence_hit: Mapping[str, object]) -> bool:
    source_status = evidence_hit.get("source_status")
    if isinstance(source_status, str) and source_status != "ok":
        return False
    return evidence_hit.get("sizing_eligible") is not False


def _selection_candidate(
    item: _ScreenedCandidateInput,
    *,
    sector: str,
    outlook_status: str | None,
    selection_lane: str | None,
    selection_metrics: Mapping[str, object],
    recommendation_lane: str | None = None,
) -> dict[str, object]:
    market_cap = item.market_cap_oku
    eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
    return {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": sector,
        "outlook_sector": outlook_status,
        "market_cap_oku": market_cap,
        "evidence_hits": item.evidence_hits,
        "independent_evidence_count": len(eligible_evidence_hits),
        "freshness_warnings": item.freshness_warnings,
        "selection_lane": selection_lane,
        "recommendation_lane": recommendation_lane,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(market_cap),
    }


def _macro_rank(status: str | None) -> int:
    match status:
        case "supportive":
            return 0
        case "neutral":
            return 1
        case _:
            return 2


def _lane_rank(name: str | None) -> int:
    order = {
        "valuation-reversion": 0,
        "strict-net-cash-discount": 1,
        "fcf-yield-discount": 2,
        "cash-rich-asset-discount": 3,
        "cashflow-yield-discount": 4,
        "sales-discount-growth": 5,
    }
    return order.get(name or "", 99)


def _evidence_strength_key(name: str, metrics: Mapping[str, object]) -> tuple[float, ...]:
    match name:
        case "valuation-reversion":
            return (
                _float_or(metrics.get("condition_a_sector_median_gap"), 1.0),
                _float_or(metrics.get("condition_a_self_range_percentile"), 1.0),
                _float_or(metrics.get("condition_b_sigma_gap"), 1.0),
                _float_or(metrics.get("price_change_60d"), 1.0),
            )
        case "cash-rich-asset-discount":
            return (
                -_float_or(metrics.get("cash_to_market_cap"), 0.0),
                _float_or(metrics.get("price_to_equity"), 99.0),
            )
        case "strict-net-cash-discount":
            return (
                -_float_or(metrics.get("net_cash_to_market_cap"), 0.0),
                _float_or(metrics.get("price_to_equity"), 99.0),
            )
        case "cashflow-yield-discount":
            return (
                -_float_or(metrics.get("ocf_yield"), 0.0),
                -_float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "fcf-yield-discount":
            return (
                -_float_or(metrics.get("fcf_yield"), 0.0),
                -_float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "sales-discount-growth":
            operating_profit = _float_or(metrics.get("operating_profit"), -1.0)
            return (
                _float_or(metrics.get("ps_sector_gap"), 1.0),
                -_float_or(metrics.get("sales_yoy"), 0.0),
                0.0 if operating_profit >= 0 else 1.0,
            )
        case _:
            return (0.0,)


def _metric_map(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float_or(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _evidence_hits_summary(
    candidates: Sequence[ScreenedCandidate],
    rules: ScreeningRules,
) -> dict[str, int]:
    summary = dict.fromkeys(rules.lane_order, 0)
    for candidate in candidates:
        for evidence_hit in candidate.evidence_hits:
            summary[evidence_hit.name] = summary.get(evidence_hit.name, 0) + 1
    return summary


def _required_ttm_non_exact_count(
    financials: Iterable[FinancialSnapshot],
    rules: ScreeningRules,
) -> int:
    snapshots = tuple(financials)
    required_qualities: list[TTMQuality] = []
    for lane in rules.screening_playbooks.values():
        if isinstance(lane, CashflowYieldLane) and lane.ttm_cfo_required:
            required_qualities.extend(snapshot.ttm_quality_ocf_yield for snapshot in snapshots)
        if isinstance(lane, FcfYieldLane) and lane.fcf_required:
            required_qualities.extend(snapshot.ttm_quality_fcf_yield for snapshot in snapshots)
        if isinstance(lane, SalesDiscountGrowthLane):
            required_qualities.extend(snapshot.ttm_quality_p_s for snapshot in snapshots)
    return sum(1 for quality in required_qualities if quality != TTMQuality.EXACT)


def _index_next_earnings(
    records: Sequence[Mapping[str, object]], asof_date: date
) -> dict[str, date]:
    # Pick the soonest forthcoming earnings announcement (>= asof_date) per
    # ticker so research packets can populate next_earnings_date for the
    # decision-period kill switch.
    asof_iso = asof_date.isoformat()
    by_ticker: dict[str, str] = {}
    for record in records:
        raw_code = (
            record.get("Code")
            or record.get("code")
            or record.get("LocalCode")
            or record.get("local_code")
        )
        if not raw_code:
            continue
        code = str(raw_code).strip().upper()
        ticker = code[:4] if len(code) == 5 else code
        if not ticker.isalnum() or len(ticker) != 4:
            continue
        raw_date = (
            record.get("Date")
            or record.get("date")
            or record.get("AnnouncementDate")
            or record.get("announcement_date")
        )
        if not raw_date:
            continue
        date_iso = str(raw_date)[:10]
        if date_iso < asof_iso:
            continue
        if ticker not in by_ticker or date_iso < by_ticker[ticker]:
            by_ticker[ticker] = date_iso
    return {ticker: date.fromisoformat(value) for ticker, value in by_ticker.items()}


def rebuild_cache_command(
    *,
    raw_dir: Path,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Rebuild the SQLite cache from raw JSON. The destination file is removed
    first so the rebuild is deterministic.
    """
    out = stdout if stdout is not None else sys.stdout
    if not raw_dir.exists():
        print(f"raw JSON directory not found: {raw_dir}", file=sys.stderr)
        return 1
    try:
        summary = rebuild_from_raw(raw_dir, sqlite_path)
    except SQLiteCacheError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"rebuilt {sqlite_path}:", file=out)
    print(
        f"  jquants_daily_bars: {summary.daily_bars_files} files / {summary.daily_bars_rows} rows",
        file=out,
    )
    print(
        f"  jquants_fin_summaries: {summary.fin_summary_files} files / "
        f"{summary.fin_summary_rows} rows",
        file=out,
    )
    print(
        f"  jquants_master_snapshots: {summary.master_files} files / {summary.master_rows} rows",
        file=out,
    )
    print(
        f"  jquants_earnings_calendar: {summary.earnings_calendar_files} files / "
        f"{summary.earnings_calendar_rows} rows",
        file=out,
    )
    print(
        f"  jquants_market_calendar: {summary.market_calendar_files} files / "
        f"{summary.market_calendar_rows} rows",
        file=out,
    )
    print(
        f"  edinet_documents: {summary.edinet_document_files} files / "
        f"{summary.edinet_document_rows} rows",
        file=out,
    )
    print(
        f"  edinet_metrics: {summary.edinet_metric_files} files / "
        f"{summary.edinet_metric_rows} rows",
        file=out,
    )
    print(
        f"  jpx_regulation_flags: {summary.jpx_regulation_files} files / "
        f"{summary.jpx_regulation_rows} rows",
        file=out,
    )
    if summary.skipped_files:
        joined = ", ".join(summary.skipped_files)
        print(f"skipped {len(summary.skipped_files)} unrecognized files: {joined}", file=out)
    return 0


def verify_cache_coverage_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    require_edinet_metrics: bool = True,
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
        require_edinet_metrics=require_edinet_metrics,
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
    try:
        while cursor <= asof_date:
            documents.extend(provider.list_documents(cursor))
            cursor += timedelta(days=1)
    except EDINETProviderError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    candidates = select_document_candidates(documents)
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
        except (EDINETProviderError, OSError, ValueError) as exc:
            hard_failure_count += 1
            record = EdinetMetricRecord(
                ticker=candidate.ticker,
                source_doc_id=candidate.doc_id,
                document_type=candidate.doc_type_code,
                source_submit_datetime=candidate.submit_datetime,
                source_period_start=candidate.period_start,
                source_period_end=candidate.period_end,
                failure_reasons=(f"csv_parse_failed:{type(exc).__name__}",),
            )
            parse_failed = True
        if record.failure_reasons and not parse_failed:
            quality_issue_count += 1
        records.append(record)

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


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def bootstrap_cache_command(
    start: date | None = None,
    end: date | None = None,
    providers: ProviderBundle | None = None,
    *,
    asof_date: date | None = None,
) -> int:
    if providers is None:
        raise ValueError("bootstrap_cache_command requires providers")
    if asof_date is not None:
        bars_start = asof_date - timedelta(days=1200)
        fin_start = asof_date - timedelta(days=730)
        earnings_end = asof_date + timedelta(days=90)
        try:
            providers.jquants.get_eq_master()
            providers.jquants.get_eq_bars_daily_range(bars_start, asof_date)
            providers.jquants.get_fin_summary_range(fin_start, asof_date)
            providers.jquants.get_eq_earnings_cal(asof_date, earnings_end)
            providers.jquants.get_mkt_calendar(asof_date, asof_date)
            if providers.edinet is not None:
                providers.edinet.bootstrap_cache(fin_start, asof_date)
            providers.jpx.bootstrap_cache(asof_date)
        except (JQuantsProviderError, EDINETProviderError, JPXProviderError, sqlite3.Error) as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    if start is None or end is None:
        raise ValueError("bootstrap_cache_command requires asof_date or start/end")
    try:
        providers.jquants.bootstrap_cache(start, end)
        if providers.edinet is not None:
            providers.edinet.bootstrap_cache(start, end)
        providers.jpx.bootstrap_cache(end)
    except (JQuantsProviderError, EDINETProviderError, JPXProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
