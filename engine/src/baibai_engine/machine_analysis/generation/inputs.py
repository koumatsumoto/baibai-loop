"""一回の固定market/economic snapshotを共有してモデルを生成する読取合成。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGenerationStream
from baibai_engine.machine_analysis.generation.runners.market_reading import reading_snapshot
from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    build_market_snapshot,
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.generation.runners.security_context import (
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.history import (
    VALUATION_HISTORY_SESSIONS,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.market_context.benchmark import NIKKEI225_ETF_PROXY
from baibai_engine.machine_analysis.models.market_context.price_path import compare_price_path
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observation_population,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
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
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import SealedSQLiteSnapshot
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def read_and_generate_models(
    *,
    generation_id: str,
    market_snapshot: SealedSQLiteSnapshot,
    market_release: InputRelease,
    economic_path: Path | None,
    economic_identity: InputSnapshot | None,
    as_of: date,
    evaluation_time: datetime,
    ttm_rules: TTMRules,
    tickers: Sequence[str] | None = None,
    growth_start: date | None = None,
) -> ModelGenerationStream:
    """callerは同じmarket sealからreleaseを作り、economic copyも固定する。

    seal内に残る旧originでなく、今回exportしたreleaseを入力identityとする。
    ここでは原本取得・候補選択・公開をせず、業種参照は全masterを維持する。
    """
    if (economic_path is None) != (economic_identity is None):
        raise ValueError("economic snapshot path and identity must be paired")
    if (
        economic_identity is not None
        and economic_path is not None
        and (
            economic_identity.kind != "sqlite_snapshot"
            or sha256_file(economic_path) != economic_identity.sha256
        )
    ):
        raise ValueError("economic snapshot differs from its fixed identity")
    path = market_snapshot.path
    master = read_master_snapshot(path, as_of=as_of)
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
        for row in read_financial_documents(connection, start=date.min, end=date.max):
            documents[row.ticker].append(row)
        calendar = read_price_calendar(connection, as_of=as_of)
        path_calendar = price_calendar(calendar, as_of=as_of)
        state_start = min(
            (window[0] for window in path_calendar.windows.values() if window is not None),
            default=path_calendar.expected_price_date or as_of,
        )
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
    prices = read_price_bars(path, start=date.min, end=as_of)
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
        self_range_window_bars=VALUATION_HISTORY_SESSIONS,
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
        path, tickers=subjects, cutoff=evaluation_time, include_tradingview=False
    )
    edinet = EDINETReader(path).load_metric_records(as_of)
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
            **({"economic": economic_identity} if economic_identity is not None else {}),
        },
        financial_release=market_release,
        companies=companies,
        trading_dates=path_calendar.trading_dates,
        expected_price_date=path_calendar.expected_price_date,
        ttm_rules=ttm_rules,
        external_observations=histories,
        market_context={
            "market": build_market_snapshot(sqlite_path=path, asof_date=as_of, prepared=prepared),
            "reading": None
            if economic_path is None
            else reading_snapshot(economic_path, asof=as_of),
        },
        securities_context=contexts,
        market_context_revision="shared-market-context-price-path-v2",
        growth_start=growth_start,
    )
