"""既存store fixtureを生成と同じ共有価格入口へ渡す。"""

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.generation.runners.security_context import (
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.calendar_context import read_price_calendar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def security_context_from_store(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    def read_calendar():
        with closing(connect_read_only(path)) as connection:
            return read_price_calendar(connection, as_of=as_of)

    calendar = price_calendar(read_store_if_written(path, read_calendar) or {}, as_of=as_of)
    prices = read_price_bars(path, start=date.min, end=as_of)
    prepared = prepare_market_context_from_bars(
        sqlite_path=path,
        asof_date=as_of,
        master=read_master_snapshot(path, as_of=as_of),
        bars_by_ticker=prices,
        subjects=(ticker,),
    )
    return build_security_market_contexts(
        sqlite_path=path,
        tickers=(ticker,),
        asof_date=as_of,
        prepared=prepared,
        price_bars_by_ticker=prices,
        calendar=calendar,
    )[ticker]
