from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_SQLITE_CACHE_DIR,
    LEGACY_CACHE_DIR,
    PARTIAL_WARNING_TTM_COUNT,
    PARTIAL_WARNING_TTM_RATIO,
    PARTIAL_WARNING_YOY_MISSING_RATIO,
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
from .rules import evaluate_screening
from .schema import ScreenedRunDocument, ScreenedTicker, SecurityMaster, normalize_ticker
from .sqlite_cache import SQLiteCacheError, rebuild_from_raw
from .tiers import MIN_AVG_TURNOVER_OKU, MIN_MARKET_CAP_OKU, position_tier
from .universe import (
    LISTED_UNDER_DAYS,
    REQUIRED_JPX_FLAGS,
    build_universe,
)
from .verify import DEFAULT_MAX_FILE_SIZE_MB, verify_raw_cache


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
    edinet: EDINETAdapter
    jpx: JPXAdapter


class _ScreenedCandidateInput(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    ticker: str
    name: str | None = None
    sector_33: str = ""
    market_cap_oku: int | float | None = None
    threshold_hit: list[str] = Field(default_factory=list)
    next_earnings_date: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


class _ScreenedFrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    tickers: list[_ScreenedCandidateInput] = Field(default_factory=list)


class _OutlookFrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    sectors: Mapping[str, str | None] = Field(default_factory=dict)


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
        help="rank research candidates by combining screened with outlook sectors",
    )
    select_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    select_parser.add_argument(
        "--outlook",
        help=(
            "outlook path to apply (default: latest "
            "records/02-outlook/<YYYY>/<MM>/outlook-*.md on or before asof)"
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
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "select":
        # select reads existing screened YAML and outlook markdown only, no env or providers needed.
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
    providers = ProviderBundle(
        jquants=JQuantsProvider(
            config.jquants_refresh_token,
            config.cache_dir,
            sqlite_path=sqlite_path,
        ),
        edinet=EDINETProvider(
            config.edinet_api_key,
            config.cache_dir,
            sqlite_path=sqlite_path,
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
    now: datetime | None = None,
    allow_stale_jpx: bool = False,
) -> int:
    output_path = build_output_path(asof_date)
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

    start_date = asof_date - timedelta(days=1200)
    try:
        calendar_days = providers.jquants.get_mkt_calendar(asof_date, asof_date)
        if not any(day.day == asof_date and day.is_business_day for day in calendar_days):
            print(f"--asof must be a business day: {asof_date.isoformat()}", file=sys.stderr)
            return 1
        securities = providers.jquants.get_eq_master()
        bars = providers.jquants.get_eq_bars_daily_range(start_date, asof_date)
        summaries = providers.jquants.get_fin_summary_range(start_date, asof_date)
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

    bars_by_ticker = group_bars_by_ticker(bars)
    summaries_by_ticker = group_summaries_by_ticker(summaries)
    next_earnings_by_ticker = _index_next_earnings(earnings_records, asof_date)
    shares_by_ticker = build_shares_outstanding_index(summaries_by_ticker)
    edinet_load_error: str | None = None
    try:
        edinet_by_ticker = providers.edinet.load_metric_records(asof_date)
    except (EDINETProviderError, OSError, ValueError) as exc:
        # cache 破損 / JSON 不正 / IO 失敗を明示的にログする。pre-existing の
        # empty cache (FileNotFoundError 相当) は load_metric_records 側で {} を返す
        # ため、ここに来るのは本質的に異常系のみ。
        edinet_load_error = f"{type(exc).__name__}: {exc}"
        print(f"warning: EDINET load_metric_records failed: {edinet_load_error}", file=sys.stderr)
        edinet_by_ticker = {}

    universe_result = build_universe(
        asof_date=asof_date,
        securities=securities,
        bars_by_ticker=bars_by_ticker,
        shares_outstanding_by_ticker=shares_by_ticker,
        jpx_flags_by_ticker=jpx_snapshot.flags_by_ticker,
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
    )

    screened_tickers: list[ScreenedTicker] = []
    fact_lines: list[str] = []
    for ticker in sorted(universe_result.snapshots):
        result = evaluate_screening(metric_result.financials[ticker], metric_result.derived[ticker])
        if not result.pass_fail:
            continue
        if len(result.threshold_hit) > 1:
            fact_lines.append(f"{ticker}: 複数閾値 hit ({', '.join(result.threshold_hit)})")
        financial = metric_result.financials[ticker]
        derived = metric_result.derived[ticker]
        universe_snapshot = universe_result.snapshots[ticker]
        security = securities_by_ticker[ticker]
        metrics_breakdown: dict[str, dict[str, float | None]] = {}
        for metric in ("per_trailing", "pbr", "ev_ebitda"):
            metrics_breakdown[metric] = {
                "sector_median_gap": derived.sector_median_gap.get(metric),
                "self_range_percentile": derived.self_range_percentile.get(metric),
                "sigma_gap": derived.sigma_gap.get(metric),
            }
        screened_tickers.append(
            ScreenedTicker(
                ticker=ticker,
                name=security.name,
                per_forward=financial.per_forward,
                per_trailing=financial.per_trailing,
                pbr=financial.pbr,
                ev_ebitda=financial.ev_ebitda,
                p_s=financial.p_s,
                pcfr=financial.pcfr,
                sector_33=security.sector_33,
                threshold_hit=result.threshold_hit,
                ttm_quality={
                    "ev_ebitda": financial.ttm_quality_ev_ebitda,
                    "p_s": financial.ttm_quality_p_s,
                    "pcfr": financial.ttm_quality_pcfr,
                },
                market_cap_oku=universe_snapshot.market_cap_oku,
                avg_turnover_oku=universe_snapshot.avg_turnover_oku,
                price_change_60d=derived.price_change_60d,
                price_change_4w=derived.ticker_return_4w,
                sector_relative_strength_percentile=derived.sector_relative_strength_percentile,
                metrics_breakdown=metrics_breakdown,
                next_earnings_date=next_earnings_by_ticker.get(ticker),
            )
        )

    universe_size = len(universe_result.snapshots)
    approx_total = metric_result.ttm_quality_counts.get(
        "approximated", 0
    ) + metric_result.ttm_quality_counts.get("unavailable", 0)
    partial_warning = universe_size > 0 and (
        approx_total >= PARTIAL_WARNING_TTM_COUNT
        or (approx_total / universe_size) >= PARTIAL_WARNING_TTM_RATIO
        or (metric_result.yoy_missing_count / universe_size) >= PARTIAL_WARNING_YOY_MISSING_RATIO
    )
    fallback_lines: list[str] = []
    if approx_total:
        fallback_lines.append(f"ttm_quality 非 exact 件数: {approx_total}")
    if metric_result.yoy_missing_count:
        fallback_lines.append(f"業績悪化フィルタ入力欠損: {metric_result.yoy_missing_count} 銘柄")
    if edinet_load_error is not None:
        fallback_lines.append(f"EDINET 読み込み失敗: {edinet_load_error}")
    # JPX source coverage: REQUIRED_JPX_FLAGS 全てを載せきれていないなら明示する。
    missing_jpx_sources = tuple(
        flag for flag in sorted(REQUIRED_JPX_FLAGS) if flag not in jpx_snapshot.source_names
    )
    if missing_jpx_sources:
        fallback_lines.append(f"JPX source 未ロード: {', '.join(missing_jpx_sources)}")

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

    document = ScreenedRunDocument(
        run_date=asof_date,
        asof_date=asof_date,
        universe_size=universe_size,
        filters={
            "min_market_cap_oku": MIN_MARKET_CAP_OKU,
            "min_avg_turnover_oku": MIN_AVG_TURNOVER_OKU,
            "exclude_listed_under_months": LISTED_UNDER_DAYS // 30,
        },
        tickers=tuple(screened_tickers),
        run_at=run_now,
        run_id=run_id,
        config_hash=config_hash,
        cache_manifest_hash=cache_manifest_hash,
        fact_memo_lines=tuple(fact_lines),
        provider_status_lines=(
            "データソース: J-Quants Light（日足・財務サマリー・業績予想）+ EDINET + JPX",
        ),
        universe_exclusion_lines=tuple(
            f"{reason}: {count} 件" for reason, count in universe_result.exclusion_counts.items()
        ),
        ttm_quality_counts=metric_result.ttm_quality_counts,
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
    screened_root: Path | None = None,
    outlook_root: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    screened_root = screened_root or Path("records/03-screened")
    outlook_root = outlook_root or Path("records/02-outlook")

    screened_path = (
        screened_root / f"{asof_date:%Y}" / f"{asof_date:%m}" / f"{asof_date:%Y-%m-%d}.yaml"
    )
    if not screened_path.exists():
        print(f"screened file not found: {screened_path}", file=sys.stderr)
        return 1
    try:
        screened_fm = TypeAdapter(_ScreenedFrontMatter).validate_python(
            _parse_screened_yaml_payload(screened_path)
        )
    except (ValidationError, ValueError) as exc:
        print(f"invalid screened YAML: {screened_path}: {exc}", file=sys.stderr)
        return 1

    resolved_outlook_path = outlook_path or _find_latest_outlook(outlook_root, asof_date)
    if resolved_outlook_path is None or not resolved_outlook_path.exists():
        print(
            "outlook file not found. Pass --outlook <path> or create "
            "records/02-outlook/<YYYY>/<MM>/outlook-*.md",
            file=sys.stderr,
        )
        return 1
    try:
        outlook_fm = TypeAdapter(_OutlookFrontMatter).validate_python(
            _parse_markdown_front_matter(resolved_outlook_path)
        )
    except ValidationError as exc:
        print(f"invalid outlook front matter: {resolved_outlook_path}: {exc}", file=sys.stderr)
        return 1

    candidates = _rank_candidates(screened_fm.tickers, outlook_fm.sectors)
    summary = {
        "asof": asof_date.isoformat(),
        "screened_ref": str(screened_path),
        "outlook_ref": str(resolved_outlook_path),
        "input_count": len(screened_fm.tickers),
        "after_outlook_filter": len(candidates),
        "candidates": candidates[:top],
    }
    yaml.safe_dump(summary, out, allow_unicode=True, sort_keys=False)
    return 0


def _parse_markdown_front_matter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def _parse_screened_yaml_payload(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"screened YAML root must be a mapping: {path}")
    return payload


def _find_latest_outlook(outlook_root: Path, asof_date: date) -> Path | None:
    if not outlook_root.exists():
        return None
    asof_iso = asof_date.isoformat()
    matches = sorted(outlook_root.glob("*/*/outlook-*.md"))
    eligible = [
        path
        for path in matches
        # Heuristic: outlook filename contains a YYYY-MM-DD on or before asof.
        if (m := re.search(r"\d{4}-\d{2}-\d{2}", path.name)) and m.group(0) <= asof_iso
    ]
    return eligible[-1] if eligible else None


def _rank_candidates(
    tickers: list[_ScreenedCandidateInput],
    sectors_outlook: Mapping[str, str | None],
) -> list[dict[str, object]]:
    ranked: list[tuple[tuple[int, int, str], dict[str, object]]] = []
    for ticker in tickers:
        sector = ticker.sector_33
        outlook_status = sectors_outlook.get(sector)
        # headwind は除外。null / unknown / tailwind / neutral は通過。
        if outlook_status == "headwind":
            continue
        market_cap = ticker.market_cap_oku
        market_cap_int = int(market_cap) if isinstance(market_cap, (int, float)) else 0
        # Higher = better: more threshold hits, larger market cap, then ticker tie-breaker
        sort_key = (
            -len(ticker.threshold_hit),
            -market_cap_int,
            ticker.ticker,
        )
        candidate: dict[str, object] = {
            "ticker": ticker.ticker,
            "name": ticker.name,
            "sector_33": sector,
            "outlook_sector": outlook_status,
            "market_cap_oku": market_cap,
            "threshold_hit": ticker.threshold_hit,
            "threshold_hit_count": len(ticker.threshold_hit),
            "next_earnings_date": ticker.next_earnings_date,
            "position_tier": position_tier(market_cap),
        }
        ranked.append((sort_key, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]


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
