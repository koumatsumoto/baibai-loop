"""既存store fixtureを生成と同じ共有価格入口へ渡す。"""

from collections.abc import Sequence
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
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from tests.helpers.master_calendar_native import FixtureCompany


def fixture_market_master(as_of: date, companies: Sequence[FixtureCompany]) -> MasterSnapshot:
    """Construct the already admitted current membership for pure calculation tests."""
    return MasterSnapshot(
        as_of,
        {
            ticker: SecurityMaster(ticker, name, segment, sector, category == "011")
            for ticker, name, segment, sector, category in companies
        },
    )


def security_context_from_store(
    path: Path, *, ticker: str, as_of: date, master: MasterSnapshot | None = None
) -> dict[str, object]:
    def read_calendar():
        with closing(connect_read_only(path)) as connection:
            return read_price_calendar(connection, as_of=as_of)

    calendar = price_calendar(read_store_if_written(path, read_calendar) or {}, as_of=as_of)
    prices = read_price_bars(path, start=date.min, end=as_of)
    prepared = prepare_market_context_from_bars(
        sqlite_path=path,
        asof_date=as_of,
        master=master,
        bars_by_ticker=prices.items(),
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
