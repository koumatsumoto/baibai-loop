"""他領域の公開fixtureにも、実nativeの暦・完全master原本を明示する。"""

import json
import sqlite3
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
from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY as MASTER_HEAD
from baibai_engine.data_collection.providers.jquants.master_native import MasterAttempt, MasterL1
from baibai_jobs.publication import snapshot_publication as publication
from tests.helpers.screening_sqlite import make_master_records


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
    from baibai_engine.data_collection.providers.jquants.receipts import read_request_pages

    with closing(sqlite3.connect(market)) as connection:
        identities = connection.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages WHERE source='market_calendar'"
        ).fetchall()
        retained = []
        for (identity,) in identities:
            pages = tuple(read_request_pages(connection, identity))
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


def seed_master(store, market: Path, directory: Path, *, as_of: date) -> MasterL1:
    """既知fixtureの行を保ち、他領域testでも完全母集団の原本を使う。"""
    owner = MasterL1(store, directory)
    if store.head(MASTER_HEAD) is not None:
        return owner
    with closing(sqlite3.connect(market)) as connection:
        raw = connection.execute(
            "SELECT snapshot_date,record_json FROM jquants_master_snapshots ORDER BY snapshot_date,security_code"
        ).fetchall()
    grouped = {}
    for day, payload in raw:
        grouped.setdefault(day, []).append(json.loads(payload))
    if not grouped:
        grouped[as_of.isoformat()] = []
    attempts = []
    for day, rows in grouped.items():
        # Master/calendar contract tests use their own exact raw attempts. Only
        # unrelated public-page/EDINET/credit fixtures use this complete input.
        identities = {row["Code"] for row in rows}
        for row in make_master_records(date.fromisoformat(day)):
            if len(rows) >= 2500:
                break
            if row["Code"] not in identities:
                rows.append(row)
                identities.add(row["Code"])
        at = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC)
        attempts.append(
            MasterAttempt(
                f"fixture-master-{day}",
                date.fromisoformat(day),
                at,
                (JQuantsPage("master", {"date": day}, at, json.dumps({"data": rows}).encode()),),
            )
        )
    owner.initialize()
    owner.commit(attempts)
    return owner


def configure_master_calendar_publication(monkeypatch):
    prepare = publication.prepare_market_lake

    def with_native_dependencies(**kwargs):
        as_of = kwargs.get("as_of")
        if as_of is None:
            with closing(sqlite3.connect(kwargs["sqlite_path"])) as connection:
                last = connection.execute(
                    "SELECT MAX(day) FROM (SELECT snapshot_date AS day FROM jquants_master_snapshots UNION ALL SELECT traded_at AS day FROM jquants_daily_bars)"
                ).fetchone()[0]
            as_of = date.fromisoformat(last) if last else date(2026, 1, 5)
            kwargs["as_of"] = as_of
        seed_calendar(
            kwargs["store"],
            kwargs["sqlite_path"],
            kwargs["mirror_root"] / "fixture-calendar",
            as_of=as_of,
        )
        seed_master(
            kwargs["store"],
            kwargs["sqlite_path"],
            kwargs["mirror_root"] / "fixture-master",
            as_of=as_of,
        )
        return prepare(**kwargs)

    monkeypatch.setattr(publication, "prepare_market_lake", with_native_dependencies)


@pytest.fixture
def native_master_calendar_publication(monkeypatch):
    configure_master_calendar_publication(monkeypatch)
