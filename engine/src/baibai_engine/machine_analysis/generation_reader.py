"""一回の固定market/economic snapshotを共有してモデルを生成する読取合成。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.jquants_reader import read_financial_documents, read_master_snapshot
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent
from baibai_engine.market.price_reader import _adjustment_events_from_rows
from baibai_engine.market.universe import ELIGIBLE_MARKETS
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.lake.writer import SealedSQLiteSnapshot
from baibai_engine.storage.observations.read import connect_read_only

from .company_growth.models import InputRelease
from .company_valuation.metrics.history import VALUATION_HISTORY_SESSIONS
from .company_valuation.metrics.models import TTMRules
from .external_assessment.reader import read_observation_population
from .generation import CompanyModelInputs, generate_models
from .market_context.reading.query import reading_snapshot
from .market_context.security import build_security_market_contexts
from .market_context.snapshot import build_market_snapshot, prepare_market_context_from_bars
from .model_generation import InputSnapshot, ModelGeneration


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
) -> ModelGeneration:
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
                for ticker, row in master.securities.items()
                if row["is_common_stock"] and str(row["market_segment"]).upper() in ELIGIBLE_MARKETS
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
        calendar = {
            date.fromisoformat(row[0]): bool(row[1])
            for row in connection.execute(
                "SELECT day, holiday_division IN ('1','2') FROM jquants_market_calendar "
                "WHERE day<=? ORDER BY day",
                (as_of.isoformat(),),
            )
        }
        expected_price_date, trading_dates = _calendar_basis(calendar, as_of=as_of)
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
    histories = read_observation_population(path, tickers=subjects, cutoff=evaluation_time)
    companies = [
        CompanyModelInputs(
            ticker=ticker,
            financial_documents=documents.get(ticker, ()),
            adjusted_bars=prices.get(ticker, ()),
            adjustment_events=events.get(ticker, ()),
            sector=None if master is None else _sector(master.securities.get(ticker)),
        )
        for ticker in subjects
    ]
    return generate_models(
        generation_id=generation_id,
        as_of=as_of,
        evaluation_time=evaluation_time,
        inputs={
            "market": InputSnapshot(
                kind="lake_release",
                reference=market_release.release_id,
                sha256=market_release.manifest_sha256,
            ),
            **({"economic": economic_identity} if economic_identity is not None else {}),
        },
        financial_release=market_release,
        companies=companies,
        trading_dates=trading_dates,
        expected_price_date=expected_price_date,
        ttm_rules=ttm_rules,
        external_observations=histories,
        market_context={
            "market": build_market_snapshot(sqlite_path=path, asof_date=as_of, prepared=prepared),
            "reading": None
            if economic_path is None
            else reading_snapshot(economic_path, asof=as_of),
        },
        securities_context=contexts,
        market_context_revision="shared-market-context-v1",
        growth_start=growth_start,
    )


def _sector(security: dict[str, object] | None) -> str | None:
    value = None if security is None else security.get("sector_33")
    return value if isinstance(value, str) else None


def _calendar_basis(
    calendar: dict[date, bool], *, as_of: date
) -> tuple[date | None, tuple[date, ...]]:
    """Yの最新日とMの253営業日を別々に確認し、穴を営業日短縮へ変えない。"""
    dates = tuple(sorted(day for day, business in calendar.items() if business and day <= as_of))
    if as_of not in calendar or not dates:
        return None, ()
    latest = dates[-1]
    if sum(latest <= day <= as_of for day in calendar) != (as_of - latest).days + 1:
        return None, ()
    if len(dates) >= 253:
        start = dates[-253]
        if sum(start <= day <= as_of for day in calendar) != (as_of - start).days + 1:
            return latest, ()
    return latest, dates
