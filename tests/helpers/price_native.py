"""実native日足を通る、他領域の公開test用合成入力。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import (
    HEAD_KEY,
    PriceAttempt,
    PriceL1,
)
from baibai_jobs.publication import snapshot_publication as publication


def seed_prices(store, market: Path, directory: Path, *, as_of: date | None = None) -> date:
    """既知の合成SQLite fixtureから合成原本を一度用意し、以後はnative正本を保つ。"""
    owner = PriceL1(store, directory)
    if store.head(HEAD_KEY) is not None:
        return max(date.fromisoformat(day) for day in owner.fixed().value()["scopes"])
    with closing(sqlite3.connect(market)) as connection:
        rows = connection.execute(
            "SELECT * FROM jquants_daily_bars ORDER BY traded_at,ticker"
        ).fetchall()
    grouped = {}
    fields = ("O", "H", "L", "C", "Vo", "Va", "AdjFactor", "UL", "LL", "ExRT", "MktCap")
    for ticker, day, *values in rows:
        grouped.setdefault(day, []).append(
            {"Code": ticker + "0", "Date": day, **dict(zip(fields, values, strict=True))}
        )
    first = date.fromisoformat(min(grouped)) if grouped else (as_of or date(2026, 1, 5))
    last = max(date.fromisoformat(max(grouped)), as_of or first) if grouped else first
    attempts = []
    for index in range((last - first).days + 1):
        day = first + timedelta(days=index)
        at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        query = {"date": day.isoformat()}
        page = JQuantsPage(
            "daily_bars", query, at, json.dumps({"data": grouped.get(day.isoformat(), [])}).encode()
        )
        attempts.append(PriceAttempt("synthetic-price-" + day.isoformat(), day, at, (page,)))
    owner.initialize()
    owner.commit(attempts)
    return last


def configure_price_publication(monkeypatch):
    """古い公開fixtureもmodel_input/exportをstubせず、固定native日足で実行する。"""
    prepare = publication.prepare_market_lake

    def with_native_prices(**kwargs):
        as_of = seed_prices(
            kwargs["store"],
            kwargs["sqlite_path"],
            kwargs["mirror_root"] / "fixture-price",
            as_of=kwargs.get("as_of"),
        )
        if kwargs.get("as_of") is None:
            kwargs["as_of"] = as_of
        return prepare(**kwargs)

    monkeypatch.setattr(publication, "prepare_market_lake", with_native_prices)


@pytest.fixture
def native_price_publication(monkeypatch):
    configure_price_publication(monkeypatch)
