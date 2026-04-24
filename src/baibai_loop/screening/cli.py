from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import (
    ConfigError,
    PARTIAL_WARNING_TTM_COUNT,
    PARTIAL_WARNING_TTM_RATIO,
    PARTIAL_WARNING_YOY_MISSING_RATIO,
    ScreeningConfig,
)
from .metrics import build_metrics, build_shares_outstanding_index, group_bars_by_ticker, group_summaries_by_ticker
from .providers import EDINETProvider, JPXProvider, JQuantsProvider
from .providers.edinet import EDINETProviderError
from .providers.jpx import JPXProviderError
from .providers.jquants import JQuantsProviderError
from .render import JST, build_output_path, render_screened_markdown
from .rules import evaluate_screening
from .schema import ScreenedRunDocument, ScreenedTicker
from .universe import MIN_AVG_TURNOVER_OKU, MIN_MARKET_CAP_OKU, LISTED_UNDER_DAYS, REQUIRED_JPX_FLAGS, build_universe


@dataclass(frozen=True)
class ProviderBundle:
    jquants: object
    edinet: object
    jpx: object


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m baibai_loop.screening.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run weekly screening")
    run_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap raw caches before provider logic is fully wired",
    )
    bootstrap_parser.add_argument("--start", required=True, help="start date (YYYY-MM-DD)")
    bootstrap_parser.add_argument("--end", required=True, help="end date (YYYY-MM-DD)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    providers = ProviderBundle(
        jquants=JQuantsProvider(config.jquants_refresh_token, config.cache_dir),
        edinet=EDINETProvider(config.edinet_api_key, config.cache_dir),
        jpx=JPXProvider(config.cache_dir, regulation_urls=config.jpx_regulation_urls),
    )

    if args.command == "run":
        return run_command(_parse_iso_date(args.asof), config, providers)

    if args.command == "bootstrap-cache":
        start = _parse_iso_date(args.start)
        end = _parse_iso_date(args.end)
        if start > end:
            print("--start must be on or before --end", file=sys.stderr)
            return 1
        return bootstrap_cache_command(start, end, providers)

    parser.error("unknown command")
    return 2


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
) -> int:
    del config
    output_path = build_output_path(asof_date)
    if output_path.exists():
        print(f"output already exists: {output_path}", file=sys.stderr)
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
        providers.jquants.get_eq_earnings_cal(start_date, asof_date)
        jpx_snapshot = providers.jpx.get_regulation_snapshot(asof_date)
    except (JQuantsProviderError, JPXProviderError) as exc:
        # 型情報を残して root cause を追いやすくする。secret を含みうる 3rd party
        # exception はラップ済みなので str(exc) 表示で安全。
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    bars_by_ticker = group_bars_by_ticker(bars)
    summaries_by_ticker = group_summaries_by_ticker(summaries)
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
        security = securities_by_ticker[ticker]
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
            )
        )

    universe_size = len(universe_result.snapshots)
    approx_total = metric_result.ttm_quality_counts.get("approximated", 0) + metric_result.ttm_quality_counts.get("unavailable", 0)
    partial_warning = (
        universe_size > 0
        and (
            approx_total >= PARTIAL_WARNING_TTM_COUNT
            or (approx_total / universe_size) >= PARTIAL_WARNING_TTM_RATIO
            or (metric_result.yoy_missing_count / universe_size) >= PARTIAL_WARNING_YOY_MISSING_RATIO
        )
    )
    fallback_lines: list[str] = []
    if approx_total:
        fallback_lines.append(f"ttm_quality 非 exact 件数: {approx_total}")
    if metric_result.yoy_missing_count:
        fallback_lines.append(f"業績悪化フィルタ入力欠損: {metric_result.yoy_missing_count} 銘柄")
    if edinet_load_error is not None:
        fallback_lines.append(f"EDINET 読み込み失敗: {edinet_load_error}")
    # JPX source coverage: REQUIRED_JPX_FLAGS 全てを載せきれていないなら明示する。
    missing_jpx_sources = tuple(flag for flag in sorted(REQUIRED_JPX_FLAGS) if flag not in jpx_snapshot.source_names)
    if missing_jpx_sources:
        fallback_lines.append(f"JPX source 未ロード: {', '.join(missing_jpx_sources)}")

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
        run_at=now or datetime.now(JST),
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
    markdown = render_screened_markdown(document)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return 2 if partial_warning else 0


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
