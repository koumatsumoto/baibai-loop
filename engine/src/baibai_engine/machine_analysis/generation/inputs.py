"""一回の固定market/economic snapshotを共有してモデルを生成する読取合成。"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGenerationStream
from baibai_engine.machine_analysis.generation.economic_inputs import EconomicModelInput
from baibai_engine.machine_analysis.generation.edinet_inputs import EdinetModelInput
from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    build_market_snapshot,
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.generation.runners.security_context import (
    SELF_RANGE_WINDOW_BARS,
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.generation.runners.trend_inputs import read_trend_inputs
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.market_context.benchmark import NIKKEI225_ETF_PROXY
from baibai_engine.machine_analysis.models.market_context.price_path import compare_price_path
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observation_population,
)
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import (
    read_capital_actions,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparison_reader import (
    read_comparison_population,
)
from baibai_engine.machine_analysis.observations.financials.edinet_holder_capital import (
    read_holder_capital,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statement_reader import (
    read_pdf_statements,
)
from baibai_engine.machine_analysis.observations.financials.reader import (
    read_financial_documents,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    read_price_calendar,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
)
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_states,
)
from baibai_engine.machine_analysis.observations.prices.price_reader import (
    _adjustment_events_from_rows,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.writer import SealedSQLiteSnapshot
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def _phase_progress(phase: str, started: float | None = None) -> float:
    now = time.perf_counter()
    state = "start" if started is None else f"done elapsed_seconds={now - started:.3f}"
    print(f"model inputs: phase={phase} state={state}", file=sys.stderr, flush=True)
    return now


@dataclass(frozen=True, slots=True)
class FinancialModelInput:
    """A fixed financial selection, independent of the market execution copy."""

    identity: InputSnapshot
    documents: Sequence[JQuantsFinancialSummary]


def read_and_generate_models(
    *,
    generation_id: str,
    market_snapshot: SealedSQLiteSnapshot,
    market_release: InputRelease,
    economic_input: EconomicModelInput | None,
    as_of: date,
    evaluation_time: datetime,
    ttm_rules: TTMRules,
    tickers: Sequence[str] | None = None,
    growth_start: date | None = None,
    financial_input: FinancialModelInput | None = None,
    edinet_input: EdinetModelInput | None = None,
    price_identity: InputSnapshot | None = None,
    master_identity: InputSnapshot | None = None,
    calendar_identity: InputSnapshot | None = None,
    margin_alerts_identity: InputSnapshot | None = None,
    margin_interest_identity: InputSnapshot | None = None,
    short_sale_reports_identity: InputSnapshot | None = None,
) -> ModelGenerationStream:
    """callerは同じmarket sealと保存した経済selectionから入力を固定する。

    seal内に残る旧originでなく、今回exportしたreleaseを入力identityとする。
    ここでは原本取得・候補選択・公開をせず、業種参照は全masterを維持する。
    """
    if economic_input is not None and (
        economic_input.as_of != as_of or economic_input.evaluation_time != evaluation_time
    ):
        raise ValueError("economic input clocks differ from the generation")
    if edinet_input is not None and (
        edinet_input.selected_at > market_snapshot.sealed_at
        or edinet_input.selected_at > evaluation_time
    ):
        raise ValueError("EDINET input was selected after the generation seal or clock")
    path = market_snapshot.path
    master_as_of: date | None = as_of
    if master_identity is not None and master_identity.descriptor is not None:
        summary = master_identity.descriptor.get("summary")
        basis = summary.get("master_basis_date") if isinstance(summary, dict) else None
        if isinstance(basis, str):
            assert isinstance(summary, dict)
            selected = summary.get("snapshot_date")
            master_as_of = date.fromisoformat(selected) if isinstance(selected, str) else None
            if date.fromisoformat(basis) > as_of or (
                master_as_of is not None and master_as_of > date.fromisoformat(basis)
            ):
                raise ValueError("master basis cannot be later than generation as-of")
    master = None if master_as_of is None else read_master_snapshot(path, as_of=master_as_of)
    if tickers is None:
        if master is None:
            raise ValueError("all-company generation requires a master snapshot")
        subjects = tuple(
            sorted(
                ticker
                for ticker, security in master.securities.items()
                if security.is_common_stock and security.market_segment.upper() in ELIGIBLE_MARKETS
            )
        )
        if not subjects:
            raise ValueError("all-company generation has no eligible common-stock universe")
    else:
        subjects = tuple(sorted({normalize_ticker(ticker) for ticker in tickers}))
    documents: dict[str, list[JQuantsFinancialSummary]] = defaultdict(list)
    events: dict[str, list[JQuantsAdjustmentFactorEvent]] = defaultdict(list)
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        financial_rows = (
            financial_input.documents
            if financial_input is not None
            else read_financial_documents(connection, start=date.min, end=date.max)
        )
        for row in financial_rows:
            documents[row.ticker].append(row)
        phase_started = _phase_progress("edinet_comparisons")
        comparisons = read_comparison_population(
            connection,
            as_of=as_of,
            tickers=subjects,
            known_at=evaluation_time,
            archives=edinet_input.archives if edinet_input is not None else None,
        )
        _phase_progress("edinet_comparisons", phase_started)
        phase_started = _phase_progress("pdf_financials")
        pdf_statements = read_pdf_statements(
            connection, as_of=as_of, tickers=subjects, known_at=evaluation_time
        )
        capital_actions = read_capital_actions(
            connection, as_of=as_of, tickers=subjects, known_at=evaluation_time
        )
        _phase_progress("pdf_financials", phase_started)
        phase_started = _phase_progress("holder_capital")
        try:
            holder_capital, holder_coverage = read_holder_capital(
                connection,
                as_of=as_of,
                tickers=subjects,
                known_at=evaluation_time,
                snapshot_sealed_at=market_snapshot.sealed_at,
                archives=edinet_input.archives if edinet_input is not None else None,
            )
        except (sqlite3.Error, EDINETProviderError, LakePublishError, ValueError, OSError) as error:
            # A failed supplemental read must not erase independent market/financial data.
            logging.getLogger(__name__).warning(
                "holder-capital unavailable: %s", type(error).__name__
            )
            holder_capital, holder_coverage = {}, "unavailable"
        else:
            _phase_progress("holder_capital", phase_started)
        calendar = read_price_calendar(connection, as_of=as_of)
        path_calendar = price_calendar(calendar, as_of=as_of)
        state_start = min(
            (window[0] for window in path_calendar.windows.values() if window is not None),
            default=path_calendar.expected_price_date or as_of,
        )
        # Keep the provider's own gross market cap as a separate observation.
        # It can lag or use another basis and is never a replacement denominator.
        provider_capital = {}
        if path_calendar.expected_price_date is not None:
            for ticker, close, market_cap in connection.execute(
                "SELECT ticker,close,market_cap_million_yen FROM jquants_daily_bars "
                "WHERE traded_at=? AND close IS NOT NULL AND market_cap_million_yen IS NOT NULL",
                (path_calendar.expected_price_date.isoformat(),),
            ):
                if not all(
                    isinstance(value, (int, float)) and isfinite(value) and value > 0
                    for value in (close, market_cap)
                ):
                    continue
                provider_capital[str(ticker)] = {
                    "price_date": path_calendar.expected_price_date.isoformat(),
                    "close_yen": close,
                    "market_cap_million_yen": market_cap,
                }
        day_states = read_price_day_states(
            connection,
            start=state_start,
            end=as_of,
            tickers=(*subjects, NIKKEI225_ETF_PROXY),
        )
        action_rows = connection.execute(
            "SELECT ticker,traded_at,adjustment_factor,ex_right_type FROM jquants_daily_bars "
            "WHERE traded_at<=? AND (adjustment_factor!=1.0 OR ex_right_type IS NOT NULL) "
            "ORDER BY ticker,traded_at",
            (as_of.isoformat(),),
        )
        for event in _adjustment_events_from_rows(action_rows):
            events[event.ticker].append(event)
    phase_started = _phase_progress("price_series")
    prices = read_price_bars(path, start=date.min, end=as_of)
    _phase_progress("price_series", phase_started)
    prepared = prepare_market_context_from_bars(
        sqlite_path=path,
        asof_date=as_of,
        master=master,
        bars_by_ticker=prices,
        subjects=subjects,
    )
    contexts = build_security_market_contexts(
        sqlite_path=path,
        tickers=subjects,
        asof_date=as_of,
        self_range_window_bars=SELF_RANGE_WINDOW_BARS,
        prepared=prepared,
        price_bars_by_ticker=prices,
    )
    # The legacy prepared benchmark is intentionally shorter. Reuse the already
    # read full adjusted series for long-path endpoints without changing old MC.
    benchmark_prices = {
        bar.traded_at: bar.price
        for bar in prices.get(prepared.benchmark_ticker, ())
        if day_states.get(prepared.benchmark_ticker, {}).get(bar.traded_at) == "priced"
    }
    price_paths = {}
    for ticker in subjects:
        path_observation = observe_price_path(
            prices.get(ticker, ()),
            calendar=path_calendar,
            day_states=day_states.get(ticker, {}),
        )
        price_paths[ticker] = path_observation.payload
        contexts[ticker]["price_path_relative"] = compare_price_path(
            path_observation.comparison_pairs,
            benchmark_prices=benchmark_prices,
            as_of=as_of,
            benchmark_ticker=prepared.benchmark_ticker,
        )
    histories = read_observation_population(
        path, tickers=subjects, cutoff=evaluation_time, include_tradingview=True
    )
    edinet = EDINETReader(path).load_metric_records(as_of)
    trends = read_trend_inputs(
        path,
        release_id=market_release.release_id,
        release_sha256=market_release.manifest_sha256,
        cutoff=evaluation_time,
        as_of=as_of,
    )
    companies = [
        CompanyModelInputs(
            ticker=ticker,
            financial_documents=documents.get(ticker, ()),
            adjusted_bars=prices.get(ticker, ()),
            adjustment_events=events.get(ticker, ()),
            sector=master.securities[ticker].sector_33
            if master is not None and ticker in master.securities
            else None,
            edinet=edinet.get(ticker),
            comparisons=comparisons.get(ticker, ()),
            pdf_statements=pdf_statements.get(ticker, ()),
            capital_actions=capital_actions.get(ticker, ()),
            holder_capital=holder_capital.get(ticker, ()),
            holder_coverage=holder_coverage,
            holder_snapshot_sealed_at=market_snapshot.sealed_at,
            provider_capital=provider_capital.get(ticker),
            price_path=price_paths[ticker],
        )
        for ticker in subjects
    ]
    return generate_models(
        generation_id=generation_id,
        as_of=as_of,
        evaluation_time=evaluation_time,
        inputs={
            "market_snapshot": InputSnapshot(
                kind="sqlite_snapshot",
                reference=market_snapshot.ref.source_id,
                sha256=market_snapshot.ref.sha256,
            ),
            "market": InputSnapshot(
                kind="lake_release",
                reference=market_release.release_id,
                sha256=market_release.manifest_sha256,
            ),
            **({"economic": economic_input.identity} if economic_input is not None else {}),
            **({"edinet": edinet_input.identity} if edinet_input is not None else {}),
            **({"calendar": calendar_identity} if calendar_identity is not None else {}),
            **(
                {"margin_alerts": margin_alerts_identity}
                if margin_alerts_identity is not None
                else {}
            ),
            **(
                {"short_sale_reports": short_sale_reports_identity}
                if short_sale_reports_identity is not None
                else {}
            ),
            **(
                {"margin_interest": margin_interest_identity}
                if margin_interest_identity is not None
                else {}
            ),
            **({"master": master_identity} if master_identity is not None else {}),
            **({"price": price_identity} if price_identity is not None else {}),
            **({"financial": financial_input.identity} if financial_input is not None else {}),
        },
        financial_release=market_release
        if financial_input is None
        else InputRelease(
            release_id=financial_input.identity.reference,
            manifest_sha256=financial_input.identity.sha256,
            collection_boundary="fixed_source_head",
        ),
        companies=companies,
        trading_dates=path_calendar.trading_dates,
        expected_price_date=path_calendar.expected_price_date,
        ttm_rules=ttm_rules,
        external_observations=histories,
        market_context={
            "market": build_market_snapshot(sqlite_path=path, asof_date=as_of, prepared=prepared),
            "benchmark_trend": None
            if prepared.benchmark_trend is None
            else prepared.benchmark_trend.to_dict(),
            **(
                {"reading": None, "tradingview_market": None}
                if economic_input is None
                else economic_input.context
            ),
        },
        securities_context=contexts,
        market_context_revision="shared-market-context-bar-timing-v7-tradingview",
        growth_start=growth_start,
        trends=trends,
    )
