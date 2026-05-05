from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from baibai_loop._env import load_project_env

from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_SQLITE_CACHE_DIR,
    LEGACY_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from .date_utils import weekday_distance
from .filesystem import write_text_atomic
from .lineage import (
    build_provider_settings,
    build_run_id,
    compute_cache_manifest,
    compute_cache_manifest_hash,
    compute_config_hash,
    write_manifest,
)
from .metrics import (
    build_metrics,
    build_shares_outstanding_index,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from .migrate import migrate_cache
from .providers import EDINETProvider, JPXProvider, JQuantsProvider
from .providers.edinet import EdinetMetricRecord, EDINETProviderError
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
from .sqlite_cache import SQLiteCacheError, is_sqlite_stale, rebuild_from_raw
from .tiers import position_tier
from .universe import (
    build_universe,
)
from .verify import DEFAULT_MAX_FILE_SIZE_MB, verify_raw_cache


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
    signals: list[dict[str, object]] = Field(default_factory=list)
    metrics: dict[str, object] = Field(default_factory=dict)
    metrics_breakdown: dict[str, object] = Field(default_factory=dict)
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
        help="allow fetching latest JPX regulation data for a stale backfill asof",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap raw caches before provider logic is fully wired",
    )
    bootstrap_parser.add_argument("--start", required=True, help="start date (YYYY-MM-DD)")
    bootstrap_parser.add_argument("--end", required=True, help="end date (YYYY-MM-DD)")

    migrate_parser = subparsers.add_parser(
        "migrate-cache",
        help="move legacy .cache/screening/ raw JSON to git-tracked records/_data/raw/screening/",
    )
    migrate_parser.add_argument(
        "--from",
        dest="source",
        default=str(LEGACY_CACHE_DIR),
        help=f"source cache dir (default: {LEGACY_CACHE_DIR})",
    )
    migrate_parser.add_argument(
        "--to",
        dest="destination",
        default=str(DEFAULT_CACHE_DIR),
        help=f"destination raw JSON dir (default: {DEFAULT_CACHE_DIR})",
    )
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the planned moves without touching the filesystem",
    )

    rebuild_parser = subparsers.add_parser(
        "rebuild-cache",
        help=(
            "rebuild SQLite cache under records/_data/cache/screening/ "
            "from records/_data/raw/screening/ JSON"
        ),
    )
    rebuild_parser.add_argument(
        "--raw-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"raw JSON root (default: {DEFAULT_CACHE_DIR})",
    )
    rebuild_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"output SQLite path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    verify_parser = subparsers.add_parser(
        "verify-raw-cache",
        help="check that records/_data/raw/screening/ stays under 50MB per file and matches SQLite",
    )
    verify_parser.add_argument(
        "--raw-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"raw JSON root (default: {DEFAULT_CACHE_DIR})",
    )
    verify_parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_FILE_SIZE_MB,
        help=(
            "fail when any single file is at or above this size in MB "
            f"(default: {DEFAULT_MAX_FILE_SIZE_MB})"
        ),
    )
    verify_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=(
            "SQLite cache to cross-check SHA-256 against raw_imports "
            f"(default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite). "
            "If the file is missing, the cross-check is skipped silently."
        ),
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
            "records/02-outlook/<YYYY>/<MM>/outlook-*.yaml on or before asof)"
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
            outlook_path=Path(args.outlook) if args.outlook else None,
            top=args.top,
        )

    if args.command == "migrate-cache":
        # migrate-cache only touches the local filesystem; no API tokens needed.
        return migrate_cache_command(
            source=Path(args.source),
            destination=Path(args.destination),
            dry_run=args.dry_run,
        )

    if args.command == "rebuild-cache":
        # rebuild-cache reads local raw JSON and writes a SQLite file; no API tokens needed.
        return rebuild_cache_command(
            raw_dir=Path(args.raw_dir),
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "verify-raw-cache":
        # verify-raw-cache only inspects local files; no API tokens needed.
        return verify_raw_cache_command(
            raw_dir=Path(args.raw_dir),
            max_size_mb=args.max_size_mb,
            sqlite_path=Path(args.sqlite_path),
        )

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sqlite_path = config.sqlite_cache_dir / "market.sqlite"
    # SQLite is the only range-aware fallback for the JSON chunk cache —
    # without it, asof-relative chunk filenames force a full 1200-day
    # refetch whenever asof shifts. Auto-rebuild before each `run` so
    # range queries always hit a fresh derived cache.
    if args.command == "run" and is_sqlite_stale((config.cache_dir,), sqlite_path):
        print(f"rebuilding SQLite cache from {config.cache_dir}…", file=sys.stderr)
        try:
            rebuild_from_raw(config.cache_dir, sqlite_path)
        except SQLiteCacheError as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    providers = ProviderBundle(
        jquants=JQuantsProvider(
            config.jquants_refresh_token,
            config.cache_dir,
            sqlite_path=sqlite_path,
        ),
        edinet=(
            EDINETProvider(
                config.edinet_api_key,
                config.cache_dir,
                sqlite_path=sqlite_path,
            )
            if config.edinet_api_key
            else None
        ),
        jpx=JPXProvider(
            config.cache_dir,
            regulation_urls=config.jpx_regulation_urls,
            special_caution_index_url=config.jpx_special_caution_index_url,
            sqlite_path=sqlite_path,
        ),
    )

    if args.command == "run":
        return run_command(
            _parse_iso_date(args.asof),
            config,
            providers,
            rules=load_screening_rules(config.rules_path),
            allow_stale_jpx=args.allow_stale_jpx,
        )

    if args.command == "bootstrap-cache":
        start = _parse_iso_date(args.start)
        end = _parse_iso_date(args.end)
        if start > end:
            print("--start must be on or before --end", file=sys.stderr)
            return 1
        return bootstrap_cache_command(start, end, providers)

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
) -> int:
    output_path = build_output_path(asof_date)
    rules = rules or load_screening_rules(config.rules_path)
    if output_path.exists():
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
    except (JQuantsProviderError, JPXProviderError) as exc:
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
    if providers.edinet is not None:
        try:
            edinet_by_ticker = providers.edinet.load_metric_records(asof_date)
        except (EDINETProviderError, OSError, ValueError) as exc:
            # cache 破損 / JSON 不正 / IO 失敗を明示的にログする。pre-existing の
            # empty cache (FileNotFoundError 相当) は load_metric_records 側で {} を返す
            # ため、ここに来るのは本質的に異常系のみ。
            edinet_load_error = f"{type(exc).__name__}: {exc}"
            print(
                f"warning: EDINET load_metric_records failed: {edinet_load_error}",
                file=sys.stderr,
            )
            edinet_by_ticker = {}

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
        if len(result.signals) > 1:
            fact_lines.append(
                f"{ticker}: 複数 signal hit ({', '.join(signal.name for signal in result.signals)})"
            )
        financial = metric_result.financials[ticker]
        derived = metric_result.derived[ticker]
        universe_snapshot = universe_result.snapshots[ticker]
        security = securities_by_ticker[ticker]
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
                signals=result.signals,
                ttm_quality={
                    "ev_ebitda": financial.ttm_quality_ev_ebitda,
                    "p_s": financial.ttm_quality_p_s,
                    "pcfr": financial.ttm_quality_pcfr,
                    "ocf_yield": financial.ttm_quality_ocf_yield,
                    "sales": financial.ttm_quality_sales,
                },
                market_cap_oku=universe_snapshot.market_cap_oku,
                avg_turnover_oku=universe_snapshot.avg_turnover_oku,
                price_change_60d=derived.price_change_60d,
                price_change_4w=derived.ticker_return_4w,
                sector_relative_strength_percentile=derived.sector_relative_strength_percentile,
                metrics={
                    "sales_ttm": financial.sales_ttm,
                    "ocf_ttm": financial.ocf_ttm,
                    "cash_eq": financial.cash_eq,
                    "total_assets": financial.total_assets,
                    "equity": financial.equity,
                    "cash_to_market_cap": financial.cash_to_market_cap,
                    "price_to_equity": financial.price_to_equity,
                    "equity_ratio": financial.equity_ratio,
                    "ocf_yield": financial.ocf_yield,
                    "sales_yoy": financial.sales_yoy,
                    "cfo_yoy": financial.cfo_yoy,
                    "operating_profit": financial.operating_profit,
                    "operating_profit_loss_narrowing": (financial.operating_profit_loss_narrowing),
                },
                metrics_breakdown=metrics_breakdown,
                next_earnings_date=next_earnings_by_ticker.get(ticker),
                split_adjustment_flag=derived.split_adjustment_flag,
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

    cache_manifest = compute_cache_manifest(config.cache_dir)
    cache_manifest_hash = compute_cache_manifest_hash(cache_manifest)
    write_manifest(
        config.cache_dir / "manifests" / f"{run_id}.json",
        cache_manifest,
        run_id=run_id,
        asof_date=asof_date,
        config_hash=config_hash,
        manifest_hash=cache_manifest_hash,
        generated_at=run_now,
        sqlite_path=config.sqlite_cache_dir / "market.sqlite",
    )

    data_sources = ["j-quants-light", "jpx-public-regulation"]
    if edinet_by_ticker:
        data_sources.append("edinet-preprocessed-metrics")
    provider_status_lines = ["データソース: J-Quants Light（日足・財務サマリー・業績予想）+ JPX"]
    if edinet_by_ticker:
        provider_status_lines.append("EDINET preprocessed metrics: loaded")
    else:
        provider_status_lines.append("EDINET preprocessed metrics: optional unavailable")

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
        signals_summary=_signals_summary(screened_candidates, rules),
        fallback_lines=tuple(fallback_lines),
    )
    yaml_text = render_screened_yaml(document)
    write_text_atomic(output_path, yaml_text)
    return 2 if partial_warning else 0


def select_command(
    *,
    asof_date: date,
    outlook_path: Path | None,
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
    candidates_root = candidates_root or Path("records/03-candidates")
    outlook_root = outlook_root or Path("records/02-outlook")
    rules = rules or load_screening_rules(_rules_path_from_env())

    candidates_path = (
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
            "records/02-outlook/<YYYY>/<MM>/outlook-*.yaml",
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
    candidates = _rank_candidates(candidates_fm.candidates, sectors_status)
    lane_toplist_limit = rules.output.lane_toplist_limit
    lane_toplists = _rank_lane_toplists(
        candidates_fm.candidates, sectors_status, lane_toplist_limit
    )
    summary = {
        "asof": asof_date.isoformat(),
        "candidates_ref": str(candidates_path),
        "outlook_ref": str(resolved_outlook_path),
        "input_count": len(candidates_fm.candidates),
        "after_outlook_filter": len(candidates),
        "selection_mode": rules.output.selection_mode,
        "lane_toplist_limit": lane_toplist_limit,
        "lane_toplists": lane_toplists,
        "candidates": candidates[:top],
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
        # headwind は除外。null / unknown / tailwind / neutral は通過。
        if outlook_status == "headwind":
            continue
        market_cap = item.market_cap_oku
        signal_count = len(item.signals)
        selection_lane, selection_metrics, strength_key = _best_selection_signal(item.signals)
        sort_key = (
            _macro_rank(outlook_status),
            _lane_rank(selection_lane),
            *strength_key,
            -signal_count,
            item.ticker,
        )
        candidate: dict[str, object] = {
            "ticker": item.ticker,
            "name": item.name,
            "sector_33": sector,
            "outlook_sector": outlook_status,
            "market_cap_oku": market_cap,
            "signals": item.signals,
            "signal_count": signal_count,
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
        "cash-rich-asset-discount": [],
        "cashflow-yield-discount": [],
        "sales-discount-growth": [],
    }
    for item in candidates_input:
        sector = item.sector_33
        outlook_status = sectors_outlook.get(sector)
        if outlook_status == "headwind":
            continue
        for signal in item.signals:
            name = _string_value(signal.get("name"))
            if name not in ranked_by_lane:
                continue
            metrics = _metric_map(signal.get("metrics"))
            sort_key = (
                _macro_rank(outlook_status),
                *_signal_strength_key(name, metrics),
                -len(item.signals),
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
                    ),
                )
            )
    output: dict[str, list[dict[str, object]]] = {}
    for name, entries in ranked_by_lane.items():
        entries.sort(key=lambda item: item[0])
        output[name] = [candidate for _, candidate in entries[:top]]
    return output


def _best_selection_signal(
    signals: Sequence[Mapping[str, object]],
) -> tuple[str | None, dict[str, object], tuple[float, ...]]:
    entries = [
        (
            _lane_rank(name),
            _signal_strength_key(name, metrics),
            name,
            metrics,
        )
        for signal in signals
        if (name := _string_value(signal.get("name"))) is not None
        for metrics in [_metric_map(signal.get("metrics"))]
    ]
    if not entries:
        return None, {}, (0.0,)
    _, strength_key, name, metrics = min(entries, key=lambda item: (item[0], item[1]))
    return name, metrics, strength_key


def _selection_candidate(
    item: _ScreenedCandidateInput,
    *,
    sector: str,
    outlook_status: str | None,
    selection_lane: str | None,
    selection_metrics: Mapping[str, object],
) -> dict[str, object]:
    market_cap = item.market_cap_oku
    return {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": sector,
        "outlook_sector": outlook_status,
        "market_cap_oku": market_cap,
        "signals": item.signals,
        "signal_count": len(item.signals),
        "selection_lane": selection_lane,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(market_cap),
    }


def _macro_rank(status: str | None) -> int:
    match status:
        case "tailwind":
            return 0
        case "neutral":
            return 1
        case _:
            return 2


def _lane_rank(name: str | None) -> int:
    order = {
        "valuation-reversion": 0,
        "cash-rich-asset-discount": 1,
        "cashflow-yield-discount": 2,
        "sales-discount-growth": 3,
    }
    return order.get(name or "", 99)


def _signal_strength_key(name: str, metrics: Mapping[str, object]) -> tuple[float, ...]:
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
        case "cashflow-yield-discount":
            return (
                -_float_or(metrics.get("ocf_yield"), 0.0),
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


def _signals_summary(
    candidates: Sequence[ScreenedCandidate],
    rules: ScreeningRules,
) -> dict[str, int]:
    summary = dict.fromkeys(rules.lane_order, 0)
    for candidate in candidates:
        for signal in candidate.signals:
            summary[signal.name] = summary.get(signal.name, 0) + 1
    return summary


def _required_ttm_non_exact_count(
    financials: Iterable[FinancialSnapshot],
    rules: ScreeningRules,
) -> int:
    snapshots = tuple(financials)
    required_qualities: list[TTMQuality] = []
    for lane in rules.signal_lanes.values():
        if isinstance(lane, CashflowYieldLane) and lane.ttm_cfo_required:
            required_qualities.extend(snapshot.ttm_quality_ocf_yield for snapshot in snapshots)
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


def migrate_cache_command(
    *,
    source: Path,
    destination: Path,
    dry_run: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """Move legacy `.cache/screening/` raw JSON to `records/_data/raw/screening/`.

    The default source / destination match the layout introduced by issue #45.
    Re-runs are idempotent: existing destination files are skipped, never
    overwritten. The source tree's empty directories are pruned at the end so
    the legacy `.cache/screening/` workspace can be removed cleanly.
    """
    out = stdout if stdout is not None else sys.stdout
    if not source.exists():
        print(f"nothing to migrate: {source} does not exist", file=out)
        return 0
    result = migrate_cache(source, destination, dry_run=dry_run)
    verb = "would move" if dry_run else "moved"
    print(
        f"{verb} {len(result.moved)} files ({result.moved_bytes} bytes); "
        f"skipped {len(result.skipped)} pre-existing files",
        file=out,
    )
    return 0


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


def verify_raw_cache_command(
    *,
    raw_dir: Path,
    max_size_mb: int,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Walk `raw_dir` and report files that exceed the size threshold or whose
    SHA-256 differs from `raw_imports.sha256` in the SQLite cache.
    """
    out = stdout if stdout is not None else sys.stdout
    if not raw_dir.exists():
        print(f"raw JSON directory not found: {raw_dir}", file=sys.stderr)
        return 1

    sqlite_arg: Path | None = sqlite_path if sqlite_path.exists() else None
    result = verify_raw_cache(raw_dir, max_size_mb=max_size_mb, sqlite_path=sqlite_arg)
    print(
        f"verified {result.file_count} files ({result.total_bytes} bytes); "
        f"largest single file {result.max_file_size} bytes",
        file=out,
    )
    if result.size_violations:
        print(f"size violations (>= {max_size_mb}MB):", file=out)
        for violation in result.size_violations:
            mb = violation.size / (1024 * 1024)
            print(f"  {violation.path} ({mb:.1f}MB)", file=out)
    if result.sqlite_issues:
        print("SQLite SHA-256 mismatches (run rebuild-cache):", file=out)
        for issue in result.sqlite_issues:
            print(
                f"  {issue.path}: expected {issue.expected_sha256} got {issue.actual_sha256}",
                file=out,
            )
    return 1 if result.has_failures else 0


def bootstrap_cache_command(start: date, end: date, providers: ProviderBundle) -> int:
    try:
        providers.jquants.bootstrap_cache(start, end)
        if providers.edinet is not None:
            providers.edinet.bootstrap_cache(start, end)
        try:
            providers.jpx.bootstrap_cache(end)
        except JPXProviderError as exc:
            # JPX は run 側で fail-fast 扱いだが、bootstrap では continue する。
            # ただし silent にせず stderr で運用者に見せる。
            print(f"warning: jpx bootstrap skipped: {exc}", file=sys.stderr)
    except (JQuantsProviderError, EDINETProviderError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
