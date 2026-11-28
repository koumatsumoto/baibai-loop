"""共通fixture用のnative暦と、その実行が固定した現行JP銘柄。"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY as CALENDAR_HEAD,
)
from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.machine_analysis.generation.inputs import PinnedGenerationMaster
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    encode_current_listing_master,
)
from baibai_jobs.integration.listing_master_current import LISTING_MASTER_CURRENT_KEY
from baibai_jobs.publication import snapshot_publication as publication


def seed_calendar(store, market: Path, directory: Path, *, as_of: date) -> CalendarL1:
    owner = CalendarL1(store, directory)
    if store.head(CALENDAR_HEAD) is not None:
        return owner
    with closing(sqlite3.connect(market)) as connection:
        rows = connection.execute(
            "SELECT day,holiday_division FROM jquants_market_calendar ORDER BY day"
        ).fetchall()
    rows = rows or [(as_of.isoformat(), "1")]
    owner.initialize()
    from tests.helpers.jquants_receipts import read_request_pages

    with closing(sqlite3.connect(market)) as connection:
        identities = connection.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages WHERE source='market_calendar'"
        ).fetchall()
        retained = []
        for (identity,) in identities:
            pages = read_request_pages(connection, identity)
            query = pages[0].query
            retained.append(
                CalendarAttempt(
                    identity,
                    date.fromisoformat(query["from"]),
                    date.fromisoformat(query["to"]),
                    None,
                    pages,
                )
            )
    if retained:
        owner.commit(retained)
        return owner
    owner.commit(
        [
            CalendarAttempt(
                f"fixture-calendar-{day}",
                date.fromisoformat(day),
                date.fromisoformat(day),
                None,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": day, "to": day},
                        datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC),
                        json.dumps({"data": [{"Date": day, "HolDiv": str(division)}]}).encode(),
                    ),
                ),
            )
            for day, division in rows
        ]
    )
    return owner


type FixtureCompany = tuple[str, str, str, str, str]


def seed_listing_session(path: Path, day: date) -> None:
    """Close the fixture's exact classification scope without filling any prices."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT OR IGNORE INTO jquants_market_calendar VALUES (?, '1')", (day.isoformat(),)
        )


def pinned_jp_listing(
    *,
    as_of: date,
    tickers: tuple[str, ...] = ("1301",),
    companies: Sequence[FixtureCompany] | None = None,
    received_at: datetime | None = None,
    output: Path | None = None,
) -> PinnedGenerationMaster:
    if companies is None:
        companies = tuple((ticker, "合成企業", "プライム", "卸売業", "011") for ticker in tickers)
    section = CurrentListingMarket(
        ListingSlice(
            "JP",
            as_of,
            tuple(
                ListingMasterRow(
                    ("XTKS", f"{ticker}0"),
                    name,
                    "common_stock" if category == "011" else "etf",
                    "listed",
                    japan=JapanListingAttributes(category, segment, sector),
                )
                for ticker, name, segment, sector, category in sorted(companies)
            ),
        ),
        "jquants-equities-master",
        received_at or datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        len(companies),
    )
    if output is not None:
        output.write_bytes(encode_current_listing_master({"JP": section}))
    return PinnedGenerationMaster(section, f"{LISTING_MASTER_CURRENT_KEY}#JP")


def configure_master_calendar_publication(monkeypatch):
    prepare = publication.prepare_market_lake

    def with_native_dependencies(**kwargs):
        as_of = kwargs.get("as_of")
        if as_of is None:
            with closing(sqlite3.connect(kwargs["sqlite_path"])) as connection:
                last = connection.execute(
                    "SELECT MAX(traded_at) FROM jquants_daily_bars"
                ).fetchone()[0]
            as_of = date.fromisoformat(last) if last else date(2026, 1, 5)
            kwargs["as_of"] = as_of
        seed_calendar(
            kwargs["store"],
            kwargs["sqlite_path"],
            kwargs["mirror_root"] / "fixture-calendar",
            as_of=as_of,
        )
        return prepare(**kwargs)

    monkeypatch.setattr(publication, "prepare_market_lake", with_native_dependencies)


@pytest.fixture
def native_master_calendar_publication(monkeypatch):
    configure_master_calendar_publication(monkeypatch)
