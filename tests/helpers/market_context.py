"""既存store fixtureを生成と同じ共有価格入口へ渡す。"""

from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.generation.runners.security_context import (
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot


def security_context_from_store(
    path: Path, *, ticker: str, as_of: date, self_range_window_bars: int
) -> dict[str, object]:
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
        self_range_window_bars=self_range_window_bars,
        prepared=prepared,
        price_bars_by_ticker=prices,
    )[ticker]
