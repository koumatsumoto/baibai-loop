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
from .universe import (
    LISTED_UNDER_DAYS,
    MIN_AVG_TURNOVER_OKU,
    MIN_MARKET_CAP_OKU,
    REQUIRED_JPX_FLAGS,
    build_universe,
)


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


class _ViewFrontMatter(BaseModel):
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

    select_parser = subparsers.add_parser(
        "select",
        help="rank research candidates by combining screened with view sectors",
    )
    select_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    select_parser.add_argument(
        "--view",
        help="view path to apply (default: latest view/<YYYY>/<MM>/view-*.md on or before asof)",
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
        # select reads existing screened YAML and view markdown only, no env or providers needed.
        return select_command(
            asof_date=_parse_iso_date(args.asof),
            view_path=Path(args.view) if args.view else None,
            top=args.top,
        )

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    providers = ProviderBundle(
        jquants=JQuantsProvider(config.jquants_refresh_token, config.cache_dir),
        edinet=EDINETProvider(config.edinet_api_key, config.cache_dir),
        jpx=JPXProvider(
            config.cache_dir,
            regulation_urls=config.jpx_regulation_urls,
            special_caution_index_url=config.jpx_special_caution_index_url,
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
    view_path: Path | None,
    top: int,
    screened_root: Path | None = None,
    view_root: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    if top < 1:
        print("--top must be greater than zero", file=sys.stderr)
        return 1

    out = stdout if stdout is not None else sys.stdout
    screened_root = screened_root or Path("screened")
    view_root = view_root or Path("view")

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

    resolved_view_path = view_path or _find_latest_view(view_root, asof_date)
    if resolved_view_path is None or not resolved_view_path.exists():
        print(
            "view file not found. Pass --view <path> or create view/<YYYY>/<MM>/view-*.md",
            file=sys.stderr,
        )
        return 1
    try:
        view_fm = TypeAdapter(_ViewFrontMatter).validate_python(
            _parse_markdown_front_matter(resolved_view_path)
        )
    except ValidationError as exc:
        print(f"invalid view front matter: {resolved_view_path}: {exc}", file=sys.stderr)
        return 1

    candidates = _rank_candidates(screened_fm.tickers, view_fm.sectors)
    summary = {
        "asof": asof_date.isoformat(),
        "screened_ref": str(screened_path),
        "view_ref": str(resolved_view_path),
        "input_count": len(screened_fm.tickers),
        "after_view_filter": len(candidates),
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


def _find_latest_view(view_root: Path, asof_date: date) -> Path | None:
    if not view_root.exists():
        return None
    asof_iso = asof_date.isoformat()
    matches = sorted(view_root.glob("*/*/view-*.md"))
    eligible = [
        path
        for path in matches
        # Heuristic: view filename contains a YYYY-MM-DD on or before asof.
        if (m := re.search(r"\d{4}-\d{2}-\d{2}", path.name)) and m.group(0) <= asof_iso
    ]
    return eligible[-1] if eligible else None


def _rank_candidates(
    tickers: list[_ScreenedCandidateInput],
    sectors_view: Mapping[str, str | None],
) -> list[dict[str, object]]:
    ranked: list[tuple[tuple[int, int, str], dict[str, object]]] = []
    for ticker in tickers:
        sector = ticker.sector_33
        view_status = sectors_view.get(sector)
        # headwind は除外。null / unknown / tailwind / neutral は通過。
        if view_status == "headwind":
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
            "view_sector": view_status,
            "market_cap_oku": market_cap,
            "threshold_hit": ticker.threshold_hit,
            "threshold_hit_count": len(ticker.threshold_hit),
            "next_earnings_date": ticker.next_earnings_date,
            "position_tier": _position_tier(market_cap_int),
        }
        ranked.append((sort_key, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]


# Tier 境界は 300 億 universe 閾値前提。Phase 3 (issue #39 R11) で
# 200 億化に伴い境界値を更新する。
def _position_tier(market_cap_oku: int) -> str:
    if market_cap_oku >= 1000:
        return "1000+ (max 2.0%)"
    if market_cap_oku >= 500:
        return "500-1000 (max 1.0%)"
    if market_cap_oku >= 300:
        return "300-500 (P-B only, max 0.5%)"
    return "below 300 (out of universe)"


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
