"""The `run` command: full screening pass producing the candidates YAML."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from baibai_loop.date_utils import weekday_distance
from baibai_loop.screening.config import (
    ScreeningConfig,
)
from baibai_loop.screening.filesystem import write_text_atomic
from baibai_loop.screening.freshness import detect_edinet_freshness_warnings, load_disclosure_events
from baibai_loop.screening.lineage import (
    build_run_id,
)
from baibai_loop.screening.metrics import (
    build_metrics,
    build_shares_outstanding_index,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from baibai_loop.screening.providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
)
from baibai_loop.screening.providers.jpx import JPXProviderError
from baibai_loop.screening.providers.jquants import (
    JQuantsProviderError,
)
from baibai_loop.screening.render import JST, build_output_path, render_screened_yaml
from baibai_loop.screening.rule_config import (
    CashflowYieldLane,
    FcfYieldLane,
    SalesDiscountGrowthLane,
    ScreeningRules,
    load_screening_rules,
)
from baibai_loop.screening.rules import evaluate_screening
from baibai_loop.screening.schema import (
    FinancialSnapshot,
    ScreenedCandidate,
    ScreenedRunDocument,
    TTMQuality,
    UniverseSnapshot,
)
from baibai_loop.screening.universe import (
    MIN_BAR_HISTORY,
    build_universe,
)

from .common import _date_iso
from .providers import ProviderBundle


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
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    output_path = output_path or build_output_path(asof_date)
    rules = rules or load_screening_rules(config.rules_path)
    if output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    run_now = now or datetime.now(JST)
    run_id = build_run_id(asof_date)
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
        print(f"screening run start: asof={asof_date.isoformat()}", file=out, flush=True)
        print("screening run jquants market_calendar: start", file=out, flush=True)
        calendar_days = providers.jquants.get_mkt_calendar(asof_date, asof_date)
        if not any(day.day == asof_date and day.is_business_day for day in calendar_days):
            print(f"--asof must be a business day: {asof_date.isoformat()}", file=sys.stderr)
            return 1
        print("screening run jquants eq_master: start", file=out, flush=True)
        securities = providers.jquants.get_eq_master()
        print(
            f"screening run jquants eq_master: {len(securities)} row(s)",
            file=out,
            flush=True,
        )
        print(
            "screening run jquants daily_bars: "
            f"{bars_start_date.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        bars = providers.jquants.get_eq_bars_daily_range(bars_start_date, asof_date)
        print(
            f"screening run jquants daily_bars: {len(bars)} row(s)",
            file=out,
            flush=True,
        )
        print(
            "screening run jquants fin_summaries: "
            f"{fin_start_date.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        summaries = providers.jquants.get_fin_summary_range(fin_start_date, asof_date)
        print(
            f"screening run jquants fin_summaries: {len(summaries)} row(s)",
            file=out,
            flush=True,
        )
        # Pull earnings calendar from asof to asof + 90 calendar days (~ 60
        # business days) so research can populate next_earnings_date and
        # surface kill-switch overlaps at packet build time.
        print(
            "screening run jquants earnings_calendar: "
            f"{asof_date.isoformat()}..{(asof_date + timedelta(days=90)).isoformat()} start",
            file=out,
            flush=True,
        )
        earnings_records = providers.jquants.get_eq_earnings_cal(
            asof_date, asof_date + timedelta(days=90)
        )
        print(
            f"screening run jquants earnings_calendar: {len(earnings_records)} row(s)",
            file=out,
            flush=True,
        )
        print("screening run jpx regulation: start", file=out, flush=True)
        jpx_snapshot = providers.jpx.get_regulation_snapshot(asof_date)
        print(
            f"screening run jpx regulation: {len(jpx_snapshot.flags_by_ticker)} ticker(s)",
            file=out,
            flush=True,
        )
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
        print("screening run edinet metrics: load start", file=out, flush=True)
        edinet_by_ticker = providers.edinet.load_metric_records(asof_date)
        print(
            f"screening run edinet metrics: {len(edinet_by_ticker)} ticker(s)",
            file=out,
            flush=True,
        )
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
    )
    # is_common_stock フィルタを明示して、同一 4 桁 code に優先株などが混じった場合の
    # dict 上書きを防ぐ (build_universe は非共通株を弾くが、snapshots に残った共通株の
    # SecurityMaster が優先株で上書きされると render の name/sector が誤る)。
    securities_by_ticker = {
        security.code: security
        for security in securities
        if security.is_common_stock and security.code in universe_result.snapshots
    }
    median_population = _liquid_median_population(universe_result.snapshots, rules)
    metric_result = build_metrics(
        asof_date=asof_date,
        securities_by_ticker=securities_by_ticker,
        bars_by_ticker=bars_by_ticker,
        summaries_by_ticker=summaries_by_ticker,
        edinet_by_ticker=edinet_by_ticker,
        rules=rules,
        median_population=median_population,
    )
    disclosure_load_result = load_disclosure_events(
        config.cache_dir / "disclosures",
        asof_date=asof_date,
    )

    screened_candidates: list[ScreenedCandidate] = []
    for ticker in sorted(universe_result.snapshots):
        result = evaluate_screening(
            metric_result.financials[ticker],
            metric_result.derived[ticker],
            rules,
            sector_33=securities_by_ticker[ticker].sector_33,
        )
        if not result.pass_fail:
            continue
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
                listing_span_days=universe_snapshot.listing_span_days,
                jpx_flags=universe_snapshot.jpx_flags,
                price_change_1d=derived.price_change_1d,
                price_change_5d=derived.price_change_5d,
                price_change_20d=derived.price_change_20d,
                price_change_60d=derived.price_change_60d,
                gap_from_52w_low=derived.gap_from_52w_low,
                turnover_spike_5d=derived.turnover_spike_5d,
                sector_relative_strength_percentile=derived.sector_relative_strength_percentile,
                price_history_sessions_750d=derived.price_history_sessions_750d,
                price_history_coverage_750d=derived.price_history_coverage_750d,
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
                next_earnings_date=next_earnings_by_ticker.get(ticker),
                split_adjustment_flag=derived.split_adjustment_flag,
                freshness_warnings=freshness_warnings,
            )
        )

    universe_size = len(universe_result.snapshots)
    approx_total = metric_result.ttm_quality_counts.get(
        "approximated", 0
    ) + metric_result.ttm_quality_counts.get("unavailable", 0)
    # Quality gates watch data degradation for the investable population so
    # the wider all-common-stock scope does not dilute the warning ratios.
    population_financials = [
        snapshot
        for ticker, snapshot in metric_result.financials.items()
        if ticker in median_population
    ]
    population_size = len(median_population)
    required_ttm_non_exact = _required_ttm_non_exact_count(population_financials, rules)
    population_yoy_missing = sum(
        1
        for snapshot in population_financials
        if snapshot.eps_yoy is None
        or snapshot.sales_yoy is None
        or snapshot.operating_profit_yoy is None
    )
    partial_warning = population_size > 0 and (
        required_ttm_non_exact >= rules.quality.partial_warning_ttm_count
        or (required_ttm_non_exact / population_size) >= rules.quality.partial_warning_ttm_ratio
        or (population_yoy_missing / population_size)
        >= rules.quality.partial_warning_yoy_missing_ratio
    )
    fallback_lines: list[str] = []
    if approx_total:
        fallback_lines.append(f"ttm_quality 非 exact 件数: {approx_total}")
    if required_ttm_non_exact:
        fallback_lines.append(
            f"有効 lane 必須 TTM metric 非 exact 件数(流動性母集団): {required_ttm_non_exact}"
        )
    if population_yoy_missing:
        fallback_lines.append(
            f"業績悪化フィルタ入力欠損(流動性母集団): {population_yoy_missing} 銘柄"
        )
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

    cap_null_count = sum(
        1 for snapshot in universe_result.snapshots.values() if snapshot.market_cap_oku is None
    )
    turnover_null_count = sum(
        1 for snapshot in universe_result.snapshots.values() if snapshot.avg_turnover_oku is None
    )
    provider_status_lines.append(
        f"Median population (selection.liquidity): {population_size} of {universe_size} in scope "
        f"(market_cap null={cap_null_count}, turnover null={turnover_null_count})"
    )

    document = ScreenedRunDocument(
        run_date=asof_date,
        asof_date=asof_date,
        universe_size=universe_size,
        filters={
            "scope": "all-common-stocks",
            "markets": "prime/standard/growth",
            "min_bar_history": MIN_BAR_HISTORY,
        },
        candidates=tuple(screened_candidates),
        run_at=run_now,
        run_id=run_id,
        data_sources=tuple(data_sources),
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
    status = "partial warning" if partial_warning else "ok"
    print(
        "screening run done: "
        f"status={status}; output={output_path}; "
        f"universe={universe_size}; candidates={len(screened_candidates)}",
        file=out,
        flush=True,
    )
    if fallback_lines:
        heading = (
            "screening run partial warning reasons:"
            if partial_warning
            else "screening run notices:"
        )
        print(heading, file=out, flush=True)
        for line in fallback_lines:
            print(f"- {line}", file=out, flush=True)
    return 2 if partial_warning else 0


def _liquid_median_population(
    snapshots: Mapping[str, UniverseSnapshot],
    rules: ScreeningRules,
) -> frozenset[str]:
    """Tickers whose facts satisfy the selection liquidity parameters.

    Sector / market medians and sector relative strength compare against this
    investable population so the screen's relative-valuation judgments stay
    anchored to liquid comparables while every common stock is evaluated. Uses
    the base-config liquidity rules; ``--profile-config`` overrides apply only
    to the selection filter, not to this population.
    """
    liquidity = rules.selection.liquidity
    required_jpx = frozenset(rules.universe.required_jpx_flags)
    return frozenset(
        ticker
        for ticker, snapshot in snapshots.items()
        if liquidity.matches(
            market_cap_oku=snapshot.market_cap_oku,
            avg_turnover_oku=snapshot.avg_turnover_oku,
            listing_span_days=snapshot.listing_span_days,
            jpx_flags=snapshot.jpx_flags,
            required_jpx_flags=required_jpx,
            require_facts=True,
        )
    )


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
