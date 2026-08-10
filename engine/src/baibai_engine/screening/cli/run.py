"""The `run` command: full screening pass producing the candidates YAML."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from baibai_engine.foundation.date_utils import weekday_distance
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.buyback_authorization import (
    ACQUISITION_PACE_MONTHS,
    build_buyback_authorization,
    read_buyback_status_filings,
    with_authorization_state,
)
from baibai_engine.screening.buyback_store import read_buyback_reports
from baibai_engine.screening.calibration.identity import rules_contract_hash
from baibai_engine.screening.candidate_build import build_screened_candidate
from baibai_engine.screening.capital_control import read_capital_control_annotations
from baibai_engine.screening.config import (
    ScreeningConfig,
)
from baibai_engine.screening.earnings_lag import (
    build_earnings_lag,
    index_calendar_announcements,
    tickers_without_calendar_rows,
)
from baibai_engine.screening.estimates import EXPECTED_RETURN_MODEL_VERSION
from baibai_engine.screening.freshness import (
    detect_edinet_freshness_warnings,
    load_disclosure_events,
)
from baibai_engine.screening.metrics import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
    VALUATION_CALCULATION_REVISION,
    VALUATION_HISTORY_SESSIONS,
    build_metrics,
    build_normalized_profit_signals,
    build_shares_outstanding_index,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from baibai_engine.screening.providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
)
from baibai_engine.screening.providers.jpx import JPXEarningsCalendarEntry, JPXProviderError
from baibai_engine.screening.providers.jquants import (
    JQuantsProviderError,
)
from baibai_engine.screening.render import render_screened_yaml
from baibai_engine.screening.rule_config import (
    CashflowYieldPlaybook,
    SalesDiscountGrowthPlaybook,
    ScreeningRules,
    load_screening_rules,
)
from baibai_engine.screening.rules import evaluate_screening
from baibai_engine.screening.run_store import (
    ScreeningRunStore,
    application_git_commit,
    unchanged_application_git_commit,
)
from baibai_engine.screening.schema import (
    FinancialSnapshot,
    ScreenedCandidate,
    ScreenedRunDocument,
    TTMQuality,
)
from baibai_engine.screening.universe import (
    MIN_BAR_HISTORY,
    build_universe,
    liquid_median_population,
)

from ..sqlite_reader import read_margin_supply_demand_inputs
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
    run_store_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    starting_commit = application_git_commit()
    out = stdout if stdout is not None else sys.stdout
    rules = rules or load_screening_rules(config.rules_path)
    if output_path is not None and output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1

    run_now = now or datetime.now(JST)
    run_id = f"screening-{asof_date:%Y%m%d}"
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

    # 窓の正本は metrics.py の BARS_INPUT_WINDOW_DAYS / FIN_INPUT_WINDOW_DAYS
    # (較正リプレイと共有)。fin を bars と同じ 1200 日にしないのは、J-Quants Light
    # で ~26 chunk (各 1-3 分) の追加取得コストが支配的になるため。
    bars_start_date = asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS)
    fin_start_date = asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS)
    normalized_start_date = asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS)
    try:
        print(f"screening run start: asof={asof_date.isoformat()}", file=out, flush=True)
        print("screening run jquants market_calendar: start", file=out, flush=True)
        calendar_days = providers.jquants.get_mkt_calendar(asof_date, asof_date)
        if not any(day.day == asof_date and day.is_business_day for day in calendar_days):
            print(f"--asof must be a business day: {asof_date.isoformat()}", file=sys.stderr)
            return 1
        print("screening run jquants eq_master: start", file=out, flush=True)
        securities = providers.jquants.get_eq_master(asof_date)
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
        print(
            "screening run jquants normalized_profit_inputs: "
            f"{normalized_start_date.isoformat()}..{asof_date.isoformat()} start",
            file=out,
            flush=True,
        )
        normalized_fy_summaries = providers.jquants.get_fy_summary_range(
            normalized_start_date, asof_date
        )
        normalized_split_bars = providers.jquants.get_adjustment_factor_bars_range(
            normalized_start_date, asof_date
        )
        print(
            "screening run jquants normalized_profit_inputs: "
            f"{len(normalized_fy_summaries)} FY row(s), "
            f"{len(normalized_split_bars)} split event(s)",
            file=out,
            flush=True,
        )
        print("screening run jpx earnings_calendar snapshot: start", file=out, flush=True)
        earnings_snapshot = providers.jpx.get_earnings_calendar_snapshot(asof_date)
        print(
            "screening run jpx earnings_calendar snapshot: "
            f"{earnings_snapshot.valid_record_count} row(s)",
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
    normalized_fy_by_ticker = group_summaries_by_ticker(normalized_fy_summaries)
    normalized_split_bars_by_ticker = group_bars_by_ticker(normalized_split_bars)
    next_earnings_by_ticker = _index_next_earnings(earnings_snapshot.entries, asof_date)
    calendar_announcements = index_calendar_announcements(earnings_snapshot.entries)
    shares_by_ticker = build_shares_outstanding_index(
        summaries_by_ticker, bars_by_ticker, asof_date
    )
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
    median_population = liquid_median_population(universe_result.snapshots, rules)
    margin_latest, margin_prior_26w = read_margin_supply_demand_inputs(
        config.sqlite_cache_dir / "market.sqlite", asof_date
    )
    buyback_filings = read_buyback_status_filings(
        config.sqlite_cache_dir / "market.sqlite", through=asof_date
    )
    # 価値実現の経路の annotation。ranking・gate・E[r] へは接続しない。
    capital_control_by_ticker = read_capital_control_annotations(
        config.sqlite_cache_dir / "market.sqlite",
        asof=asof_date,
        tickers=sorted(securities_by_ticker),
    )
    # 枠の中身は別 table から読む。提出の有無と枠の状態は別の観測なので、片方が欠けても
    # もう片方は出る。
    buyback_reports = read_buyback_reports(
        config.sqlite_cache_dir / "market.sqlite",
        tickers=sorted(securities_by_ticker),
        asof=asof_date,
        months=ACQUISITION_PACE_MONTHS,
    )
    metric_result = build_metrics(
        asof_date=asof_date,
        securities_by_ticker=securities_by_ticker,
        bars_by_ticker=bars_by_ticker,
        summaries_by_ticker=summaries_by_ticker,
        edinet_by_ticker=edinet_by_ticker,
        rules=rules,
        median_population=median_population,
        margin_latest=margin_latest,
        margin_prior_26w=margin_prior_26w,
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
        normalized_profit = build_normalized_profit_signals(
            normalized_fy_by_ticker.get(ticker, ()),
            normalized_split_bars_by_ticker.get(ticker, ()),
            asof_date,
            close=financial.market_price_yen,
            current_eps=financial.eps,
        )
        screened_candidates.append(
            build_screened_candidate(
                ticker=ticker,
                security=security,
                financial=financial,
                derived=derived,
                universe_snapshot=universe_snapshot,
                evidence_hits=result.evidence_hits if result.pass_fail else (),
                freshness_warnings=freshness_warnings,
                next_earnings_date=next_earnings_by_ticker.get(ticker),
                earnings_lag=build_earnings_lag(
                    asof=asof_date,
                    fin_latest_disclosed=financial.latest_disclosed_at,
                    announcement_date=calendar_announcements.get(ticker),
                    summaries=summaries_by_ticker.get(ticker, ()),
                ),
                normalized_per_3fy=normalized_profit.normalized_per_3fy,
                buyback_authorization=with_authorization_state(
                    build_buyback_authorization(
                        asof=asof_date,
                        latest_filing_date=(
                            None
                            if buyback_filings is None
                            else buyback_filings.latest_filing_by_ticker.get(ticker)
                        ),
                        observed_from=None
                        if buyback_filings is None
                        else buyback_filings.observed_from,
                    ),
                    buyback_reports.get(ticker, ()),
                ),
                capital_control=capital_control_by_ticker.get(ticker),
            )
        )

    universe_size = len(universe_result.snapshots)
    # カレンダー行を持たない universe ticker 数。個別企業の未公表でも出るので閾値は
    # 置かず、provider 側の欠落が起きたときに件数の急増として読めるようにするだけ。
    print(
        "screening run jpx earnings_calendar coverage: "
        f"{tickers_without_calendar_rows(tuple(universe_result.snapshots), calendar_announcements)}"
        f" of {universe_size} universe ticker(s) without a calendar row",
        file=out,
        flush=True,
    )
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
            f"有効 playbook 必須 TTM metric 非 exact 件数(流動性母集団): {required_ttm_non_exact}"
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

    data_sources = [
        "j-quants-light",
        "jpx-public-earnings-calendar",
        "jpx-public-regulation",
    ]
    if edinet_by_ticker:
        data_sources.append("edinet-preprocessed-metrics")
    if disclosure_load_result.file_count:
        data_sources.append("disclosure-title-events")
    provider_status_lines = [
        "データソース: J-Quants Light（日足・財務サマリー・業績予想）+ JPX（決算発表予定・規制）"
    ]
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
        screening_rules_hash=rules_contract_hash(
            rules.model_dump_json(),
            valuation_calculation_revision=VALUATION_CALCULATION_REVISION,
            variant="production",
            valuation_history_sessions=VALUATION_HISTORY_SESSIONS,
            bars_input_window_days=BARS_INPUT_WINDOW_DAYS,
            production_authority=True,
        ),
        er_model_version=EXPECTED_RETURN_MODEL_VERSION,
        data_sources=tuple(data_sources),
        provider_status_lines=tuple(provider_status_lines),
        universe_exclusion_lines=tuple(
            f"{reason}: {count} 件" for reason, count in universe_result.exclusion_counts.items()
        ),
        ttm_quality_counts=metric_result.ttm_quality_counts,
        evidence_hits_summary=_evidence_hits_summary(screened_candidates, rules),
        fallback_lines=tuple(fallback_lines),
    )
    publication_yaml = render_screened_yaml(document)
    raw_payload = safe_load(publication_yaml)
    if not isinstance(raw_payload, Mapping):  # pragma: no cover - renderer invariant
        raise AssertionError("screening renderer must produce a mapping")
    try:
        publication = ScreeningRunStore(
            run_store_path,
            git_commit_factory=lambda: unchanged_application_git_commit(starting_commit),
        ).publish_run(raw_payload)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"screening run publication failed: {exc}", file=sys.stderr)
        return 1
    yaml_text = render_screened_yaml(document, run_revision_id=publication.publication_id)
    if output_path is not None:
        write_text_atomic(output_path, yaml_text)
    status = "partial warning" if partial_warning else "ok"
    print(
        "screening run done: "
        f"status={status}; run_revision_id={publication.publication_id}; "
        f"output={output_path}; "
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


def _evidence_hits_summary(
    candidates: Sequence[ScreenedCandidate],
    rules: ScreeningRules,
) -> dict[str, int]:
    summary = dict.fromkeys(rules.playbook_order, 0)
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
    for playbook in rules.screening_playbooks.values():
        if isinstance(playbook, CashflowYieldPlaybook) and playbook.ttm_cfo_required:
            required_qualities.extend(snapshot.ttm_quality_ocf_yield for snapshot in snapshots)
        if isinstance(playbook, SalesDiscountGrowthPlaybook):
            required_qualities.extend(snapshot.ttm_quality_p_s for snapshot in snapshots)
    return sum(1 for quality in required_qualities if quality != TTMQuality.EXACT)


def _index_next_earnings(
    entries: Sequence[JPXEarningsCalendarEntry], asof_date: date
) -> dict[str, date]:
    # Pick the soonest forthcoming earnings announcement (>= asof_date) per
    # ticker so research theses can populate next_earnings_date for the
    # decision-period kill switch.
    by_ticker: dict[str, date] = {}
    for entry in entries:
        if entry.announcement_date < asof_date:
            continue
        previous = by_ticker.get(entry.ticker)
        if previous is None or entry.announcement_date < previous:
            by_ticker[entry.ticker] = entry.announcement_date
    return by_ticker
